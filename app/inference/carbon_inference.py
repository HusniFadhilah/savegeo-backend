"""
Inference engine for carbon estimation using pre-trained models
"""
from __future__ import annotations

import ee
import numpy as np
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timedelta
import logging

from app.inference.carbon_model import CarbonEstimationModel
from app.inference.model_registry import ModelRegistry

logger = logging.getLogger(__name__)

S2_BANDS = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12']
S2_INDEX_BANDS = ['NDVI', 'NDWI', 'NDMI', 'NBR', 'NDRE', 'EVI', 'SAVI', 'BSI', 'brightness']
GEE_LINEAR_ALGORITHMS = {'linear', 'ridge', 'lasso', 'elasticnet', 'elastic_net'}


class CarbonInferenceEngine:
    """
    Perform carbon estimation inference using pre-trained models
    """
    
    def __init__(self, model_name: Optional[str] = None, model_path: Optional[str] = None):
        """
        Initialize inference engine
        
        Args:
            model_name: Name of model to use (uses default if None)
        """
        self.registry = ModelRegistry()
        
        if model_name is None:
            model_name = self.registry.registry.get('default')
            if model_name is None:
                raise ValueError("No default model found. Train a model first.")
        
        self.model_name = model_name
        logger.info(f"Loading model: {model_name}")
        
        # Load model
        if model_path is None:
            model_path = self.registry.get_model_path(model_name)
        self.model_path = str(Path(model_path).resolve())
        self.model = CarbonEstimationModel.load(model_path)
        self.last_s2_image_count = None
        self._s2_image_counts = []
        logger.info("Inference engine ready")

    def _get_expected_features(self) -> list:
        features = self.model.feature_names or self.model.metadata.get('feature_names') or []
        if not features:
            raise ValueError(
                f"Model '{self.model_name}' does not contain feature_names metadata. "
                "Retrain/resave the model so inference can build the matching GEE bands."
            )
        return list(features)

    def ensure_gee_deployable(self):
        # Native GEE classifier (SMILE RF/GBT serialized to JSON) — GEE handles execution
        if (self.model.metadata.get("gee_algorithm_type") == "native_classifier" or
                self.model.metadata.get("gee_classifier_path")):
            return

        # Linear sklearn model — coefficients exported to GEE expression
        if hasattr(self.model.model, 'coef_') and hasattr(self.model.model, 'intercept_'):
            return

        # Non-linear with surrogate linear approximation samples saved
        training_info = self.model.metadata.get("training_info", {}) or {}
        if training_info.get("X_sample") is not None and training_info.get("y_sample") is not None:
            return

        raise ValueError(
            f"Model '{self.model_name}' with algorithm '{self.model.algorithm}' "
            "cannot be deployed directly to GEE because it is non-linear and "
            "no surrogate training samples were saved in metadata. Use a GEE-compatible "
            "linear model (linear/ridge/lasso/elastic_net) for carbon map and delta analysis, "
            "or retrain/resave the model with metadata['training_info']['X_sample'] and "
            "metadata['training_info']['y_sample']."
        )
    
    def _mask_s2_clouds(self, image: ee.Image) -> ee.Image:
        """
        Pixel-level cloud masking using the SCL (Scene Classification Layer).
    
        SCL classes removed:
            3  = cloud shadow
            8  = cloud medium probability
            9  = cloud high probability
            10 = thin cirrus
    
        References: ESA Sentinel-2 Level-2A Product Definition (2021)
        """
        scl = image.select('SCL')
        mask = (scl.neq(3)
                .And(scl.neq(8))
                .And(scl.neq(9))
                .And(scl.neq(10)))
        return image.updateMask(mask)

    def _safe_s2_composite(self, roi: ee.Geometry, start_date: str, end_date: str,
                           cloud_threshold: int = 50) -> ee.Image:
        def _build_collection(threshold, s_date=start_date, e_date=end_date):
            return (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(roi)
                .filterDate(s_date, e_date)
                .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', threshold))
                .map(self._mask_s2_clouds)
            )

        collection = _build_collection(cloud_threshold)
        size = collection.size().getInfo()
        logger.info(f"Found {size} Sentinel-2 images for {start_date} to {end_date} (threshold={cloud_threshold})")

        if size == 0:
            # Progressive fallback: relax cloud threshold before failing
            for fallback in [min(cloud_threshold + 30, 85), 90, 95]:
                if fallback <= cloud_threshold:
                    continue
                collection = _build_collection(fallback)
                size = collection.size().getInfo()
                logger.info(f"Fallback cloud_threshold={fallback}: found {size} images")
                if size > 0:
                    break

        if size == 0:
            raise ValueError(
                "No Sentinel-2 images found. Try a higher cloud threshold, "
                "wider date range, or different year."
            )

        self._s2_image_counts.append(int(size))
        self.last_s2_image_count = sum(self._s2_image_counts)
        composite = collection.median().multiply(0.0001).select(S2_BANDS)

        # Pixels covered by cloud/shadow/cirrus in every single scene within
        # [start_date, end_date] stay masked after the median reduction —
        # they render as a transparent hole in the result tile (the basemap
        # shows through underneath) and have no carbon estimate at all.
        # Fill just those null pixels from a wider +/-90 day window with a
        # relaxed cloud threshold; every pixel the primary composite already
        # has a valid value for is left untouched.
        try:
            wide_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
            wide_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=90)).strftime("%Y-%m-%d")
            wide_collection = _build_collection(max(cloud_threshold, 70), wide_start, wide_end)
            if wide_collection.size().getInfo() > 0:
                wide_composite = wide_collection.median().multiply(0.0001).select(S2_BANDS)
                composite = composite.unmask(wide_composite)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Gap-fill composite failed, keeping primary composite as-is: {e}")

        return composite

    def _add_s2_indices(self, img: ee.Image, prefix: str = '') -> ee.Image:
        p = f'{prefix}_' if prefix else ''
        ndvi = img.normalizedDifference(['B8', 'B4']).rename(p + 'NDVI')
        ndwi = img.normalizedDifference(['B3', 'B8']).rename(p + 'NDWI')
        ndmi = img.normalizedDifference(['B8', 'B11']).rename(p + 'NDMI')
        nbr = img.normalizedDifference(['B8', 'B12']).rename(p + 'NBR')
        ndre = img.normalizedDifference(['B8A', 'B5']).rename(p + 'NDRE')
        evi = img.expression(
            '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))',
            {'NIR': img.select('B8'), 'RED': img.select('B4'), 'BLUE': img.select('B2')}
        ).rename(p + 'EVI')
        savi = img.expression(
            '((NIR - RED) / (NIR + RED + 0.5)) * 1.5',
            {'NIR': img.select('B8'), 'RED': img.select('B4')}
        ).rename(p + 'SAVI')
        bsi = img.expression(
            '((SWIR1 + RED) - (NIR + BLUE)) / ((SWIR1 + RED) + (NIR + BLUE))',
            {'SWIR1': img.select('B11'), 'RED': img.select('B4'),
             'NIR': img.select('B8'), 'BLUE': img.select('B2')}
        ).rename(p + 'BSI')
        brightness = img.select(['B2', 'B3', 'B4']).reduce(ee.Reducer.mean()).rename(p + 'brightness')
        return ee.Image.cat([ndvi, ndwi, ndmi, nbr, ndre, evi, savi, bsi, brightness])

    def _build_standard_feature_stack(self, roi: ee.Geometry, year: int, start_month: int,
                                      end_month: int, cloud_threshold: int) -> ee.Image:
        start_date = f'{year}-{start_month:02d}-01'
        end_date = f'{year}-{end_month:02d}-28'
        sen2 = self._safe_s2_composite(roi, start_date, end_date, cloud_threshold)
        return sen2.addBands(self._add_s2_indices(sen2))

    def _build_robust_feature_stack(self, roi: ee.Geometry, year: int,
                                    cloud_threshold: int) -> ee.Image:
        s2_dry = self._safe_s2_composite(roi, f'{year}-04-01', f'{year}-09-30', cloud_threshold)
        s2_wet = self._safe_s2_composite(roi, f'{year - 1}-10-01', f'{year}-03-31', cloud_threshold)

        s1_col = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(roi)
            .filterDate(f'{year}-01-01', f'{year}-12-31')
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
            .filter(ee.Filter.eq('instrumentMode', 'IW'))
            .select(['VV', 'VH'])
        )

        if s1_col.size().getInfo() > 0:
            s1 = s1_col.median()
            s1_stack = s1.addBands(s1.select('VV').subtract(s1.select('VH')).rename('VV_VH_diff'))
        else:
            s1_stack = ee.Image.cat([
                ee.Image(0.0).rename('VV'),
                ee.Image(0.0).rename('VH'),
                ee.Image(0.0).rename('VV_VH_diff'),
            ])

        dem = ee.Image('USGS/SRTMGL1_003')
        idx_dry = self._add_s2_indices(s2_dry, 'dry')
        idx_wet = self._add_s2_indices(s2_wet, 'wet')
        delta_ndvi = idx_dry.select('dry_NDVI').subtract(idx_wet.select('wet_NDVI')).rename('delta_NDVI')

        return ee.Image.cat([
            s2_dry.rename([f'dry_{band}' for band in S2_BANDS]),
            idx_dry,
            s2_wet.rename([f'wet_{band}' for band in S2_BANDS]),
            idx_wet,
            delta_ndvi,
            s1_stack,
            dem.select('elevation'),
            ee.Terrain.slope(dem).rename('slope'),
            ee.Terrain.aspect(dem).rename('aspect'),
        ])

    def _build_s2_dem_terrain_feature_stack(self, roi: ee.Geometry, year: int, start_month: int,
                                            end_month: int, cloud_threshold: int) -> ee.Image:
        """standard_s2 (19 features) + SRTM elevation/slope/aspect/TPI/TRI/local elevation
        mean+std (7 terrain features) + 3 interaction terms (NDVI_x_elevation,
        NDMI_x_slope, B8_x_B11) = 29 features. Matches
        training/indonesia_wide_carbon_experiments/features_gee.py::build_dem +
        build_interactions exactly (same SRTM source, same 5-pixel-radius square kernel
        for local mean/std/TPI/TRI) — serves models trained via
        indonesia_wide_train.py --feature-set s2_dem (see
        evaluation_results/indonesia_wide_carbon_retraining/report.md). Distinguished from
        _build_s2_dem_landcover_feature_stack (no TPI/TRI/local-stats/landcover there) by
        the TPI/TRI/elevation_local_mean markers checked in _build_predictors.
        """
        start_date = f'{year}-{start_month:02d}-01'
        end_date = f'{year}-{end_month:02d}-28'
        sen2 = self._safe_s2_composite(roi, start_date, end_date, cloud_threshold)
        stack = sen2.addBands(self._add_s2_indices(sen2))

        dem = ee.Image('USGS/SRTMGL1_003').select('elevation')
        terrain = ee.Terrain.products(dem)
        elevation = terrain.select('elevation').rename('elevation')
        slope = terrain.select('slope').rename('slope')
        aspect = terrain.select('aspect').rename('aspect')

        kernel = ee.Kernel.square(radius=5, units='pixels')
        local_mean = elevation.reduceNeighborhood(ee.Reducer.mean(), kernel).rename('elevation_local_mean')
        local_std = elevation.reduceNeighborhood(ee.Reducer.stdDev(), kernel).rename('elevation_local_std')
        tpi = elevation.subtract(local_mean).rename('TPI')
        diff_sq = elevation.subtract(local_mean).pow(2)
        tri = diff_sq.reduceNeighborhood(ee.Reducer.mean(), kernel).sqrt().rename('TRI')

        stack = (
            stack.addBands(elevation).addBands(slope).addBands(aspect)
            .addBands(tpi).addBands(tri).addBands(local_mean).addBands(local_std)
        )

        ndvi_x_elevation = stack.select('NDVI').multiply(stack.select('elevation')).rename('NDVI_x_elevation')
        ndmi_x_slope = stack.select('NDMI').multiply(stack.select('slope')).rename('NDMI_x_slope')
        b8_x_b11 = stack.select('B8').multiply(stack.select('B11')).rename('B8_x_B11')

        return stack.addBands(ndvi_x_elevation).addBands(ndmi_x_slope).addBands(b8_x_b11)

    def _build_s2_dem_landcover_feature_stack(self, roi: ee.Geometry, year: int, start_month: int,
                                              end_month: int, cloud_threshold: int) -> ee.Image:
        """standard_s2 (19 features) + SRTM elevation/slope/aspect + ESA WorldCover
        landcover/forest_mask = 26 features. Matches
        training/indonesia_wide_carbon_experiments/feature_stacks_gee.py::build_s2_dem_landcover
        exactly (same SRTM/WorldCover sources, same forest_mask = landcover==10 rule) —
        used to serve the Indonesia-wide models trained there (see
        evaluation_results/new_indonesia_wide_carbon_retraining/report_gee_islands.md).

        Known minor train/serve discrepancy: the training script's S2 composite does
        NOT apply SCL cloud masking (plain ee.ImageCollection.median()), while this
        inference path reuses _safe_s2_composite() (which DOES apply pixel-level SCL
        masking) for consistency with every other model already served by this class.
        Cloud masking should only improve pixel quality relative to training, not harm
        it, but is worth knowing if scores look off for very cloudy AOIs/periods.
        """
        start_date = f'{year}-{start_month:02d}-01'
        end_date = f'{year}-{end_month:02d}-28'
        sen2 = self._safe_s2_composite(roi, start_date, end_date, cloud_threshold)
        stack = sen2.addBands(self._add_s2_indices(sen2))

        dem = ee.Image('USGS/SRTMGL1_003')
        elevation = dem.select('elevation')
        slope = ee.Terrain.slope(dem).rename('slope')
        aspect = ee.Terrain.aspect(dem).rename('aspect')

        landcover = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map').rename('landcover')
        forest_mask = landcover.eq(10).rename('forest_mask')

        return stack.addBands(elevation).addBands(slope).addBands(aspect).addBands(landcover).addBands(forest_mask)

    _NEIGHBORHOOD_BANDS = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12',
                           'NDVI', 'NDWI', 'NDMI', 'NBR', 'NDRE', 'EVI', 'SAVI', 'BSI', 'brightness']

    def _build_s2_neighborhood_feature_stack(self, roi: ee.Geometry, year: int, start_month: int,
                                             end_month: int, cloud_threshold: int) -> ee.Image:
        """_build_s2_dem_landcover_feature_stack (26 features) + local-window
        (5x5, radius=2px) mean/stdDev of the 19 S2 bands/indices = 26 + 38 = 64
        features. Matches
        training/indonesia_wide_carbon_experiments/feature_stacks_gee.py::build_s2_neighborhood
        exactly (same kernel radius, same band subset) — serves the s2_neighborhood
        Indonesia-wide models (island-holdout R2 0.49-0.72, best feature stack found
        across WCMC/ORNL_AGB_BGB/OPENLANDMAP_SOC, see
        evaluation_results/new_indonesia_wide_carbon_retraining/report_gee_islands.md).
        """
        stack = self._build_s2_dem_landcover_feature_stack(roi, year, start_month, end_month, cloud_threshold)
        kernel = ee.Kernel.square(radius=2, units='pixels')
        reducer = ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True)
        neighborhood_stats = stack.select(self._NEIGHBORHOOD_BANDS).reduceNeighborhood(
            reducer=reducer, kernel=kernel, skipMasked=False
        )
        return stack.addBands(neighborhood_stats)

    def _build_predictors(self, roi: ee.Geometry, year: int, start_month: int,
                          end_month: int, cloud_threshold: int) -> ee.Image:
        expected_features = self._get_expected_features()

        if any(name.startswith(('dry_', 'wet_')) for name in expected_features):
            predictors = self._build_robust_feature_stack(roi, year, cloud_threshold)
        elif any(name in ('TPI', 'TRI', 'elevation_local_mean') for name in expected_features):
            predictors = self._build_s2_dem_terrain_feature_stack(
                roi, year, start_month, end_month, cloud_threshold
            )
        elif 'B2_mean' in expected_features or 'B2_stdDev' in expected_features:
            predictors = self._build_s2_neighborhood_feature_stack(
                roi, year, start_month, end_month, cloud_threshold
            )
        elif any(name in ('landcover', 'forest_mask') for name in expected_features):
            predictors = self._build_s2_dem_landcover_feature_stack(
                roi, year, start_month, end_month, cloud_threshold
            )
        else:
            predictors = self._build_standard_feature_stack(
                roi, year, start_month, end_month, cloud_threshold
            )
            if 'constant' in expected_features:
                predictors = ee.Image(1.0).rename('constant').addBands(predictors)

        available = set(predictors.bandNames().getInfo())
        missing = [name for name in expected_features if name not in available]
        if missing:
            raise ValueError(
                f"Model '{self.model_name}' expects feature(s) not available in the "
                f"current GEE predictor stack: {', '.join(missing)}. "
                "Use a model trained with supported Sentinel-2/Sentinel-1/DEM features "
                "or retrain the model with this inference feature set."
            )

        return predictors.select(expected_features)

 
    def _get_model_coef_and_intercept(self):
        """
        Return (coef_array, intercept_float) for the fitted sklearn model.
    
        For non-linear models a Ridge surrogate is trained on predictions from
        the original model, then its coefficients are returned.
    
        Returns:
            coef      : list[float], length == n_features (in SCALED space)
            intercept : float
        """
        self.ensure_gee_deployable()

        if hasattr(self.model.model, 'coef_') and hasattr(self.model.model, 'intercept_'):
            return self.model.model.coef_.tolist(), float(self.model.model.intercept_)
    
        logger.warning(
            f"Model '{self.model.algorithm}' is non-linear — creating a linear surrogate. "
            "Use algorithm='ridge' or 'lasso' for stable GEE deployment."
        )
        return self._create_linear_surrogate()
    
    def _load_gee_native_classifier(self) -> ee.Classifier:
        """Load GEE native classifier from serialized JSON file stored in saved_models/."""
        clf_path_rel = self.model.metadata.get("gee_classifier_path")
        if not clf_path_rel:
            raise ValueError(
                f"Model '{self.model_name}' has gee_algorithm_type=native_classifier "
                "but no gee_classifier_path in metadata. Run upgrade_gee_native.py first."
            )
        # Resolve path relative to the loaded model file. Some models are loaded
        # from the upload database and are intentionally absent from registry.json.
        saved_models_dir = Path(self.model_path).parent
        clf_path = saved_models_dir / clf_path_rel
        if not clf_path.exists():
            raise FileNotFoundError(
                f"GEE classifier file not found: {clf_path}. "
                "Run upgrade_gee_native.py to regenerate."
            )
        serialized = clf_path.read_text(encoding="utf-8")
        import ee
        return ee.deserializer.fromJSON(serialized)

    def predict_for_region(self,
                       roi: ee.Geometry,
                       year: int,
                       start_month: int,
                       end_month: int,
                       cloud_threshold: int = 50,
                       scale: int = 100) -> ee.Image:
        """
        Predict carbon stock for a region using the trained model.
    
        Changes from the original (all bugs fixed):
        1. Pixel-level SCL cloud masking instead of scene-level only.
        2. All 9 indices computed — matches data_preparation.calculate_indices().
        3. No spurious 'constant=1' band (was shifting feature-to-coefficient alignment).
        4. StandardScaler applied to GEE image pixels so they match the scaled
            space in which model.coef_ was fitted.
        5. Intercept added as a scalar rather than absorbed into a constant band.
        6. `scale` parameter ensures inference resolution matches training resolution.
    
        Args:
            roi             : Region of interest (ee.Geometry)
            year            : Year for Sentinel-2 imagery
            start_month     : Start month (1–12)
            end_month       : End month (1–12)
            cloud_threshold : Max scene-level cloud cover %; pixel-level masking
                            also applied, so this can be set higher (default 50).
            scale           : Spatial resolution in metres — set to the same value
                            used in sample_training_data(..., scale=...) to avoid
                            spectral mismatch (default 100 m).
    
        Returns:
            ee.Image with band 'carbon_estimated' (Mg C / ha)
        """
    
        gee_algo_type = self.model.metadata.get("gee_algorithm_type", "")
        # Derive type for legacy models that pre-date gee_algorithm_type field
        if not gee_algo_type:
            if self.model.metadata.get("gee_classifier_path"):
                gee_algo_type = "native_classifier"
            elif self.model.metadata.get("gee_deployable", False):
                gee_algo_type = "linear_expression"
            else:
                gee_algo_type = "server_side_only"

        logger.info(
            f"Running inference [{gee_algo_type}] for {year}-{start_month:02d} to "
            f"{year}-{end_month:02d} at {scale} m resolution"
        )
        self._s2_image_counts = []
        self.last_s2_image_count = None

        if gee_algo_type not in ("linear_expression", "native_classifier"):
            raise ValueError(
                f"Model '{self.model_name}' has gee_algorithm_type='{gee_algo_type}' "
                "and cannot produce GEE tile maps. "
                "Run upgrade_gee_native.py or use a linear/ridge/lasso model."
            )

        expected_features = self._get_expected_features()
        predictors = self._build_predictors(
            roi=roi,
            year=year,
            start_month=start_month,
            end_month=end_month,
            cloud_threshold=cloud_threshold,
        )

        if gee_algo_type == "native_classifier":
            # ── GEE SMILE classifier (RF / GBT) ─────────────────────────────
            trained_clf = self._load_gee_native_classifier()
            # Use gee_feature_names if set (may exclude 'constant' for legacy models)
            gee_features = self.model.metadata.get("gee_feature_names") or expected_features
            gee_features = [f for f in gee_features if f != "constant"]
            carbon_predicted = (
                predictors.select(gee_features)
                .classify(trained_clf)
                .max(ee.Image(0))
                .rename("carbon_estimated")
                .reproject(crs="EPSG:4326", scale=scale)
            )
            logger.info(f"Native GEE classifier applied (features={len(gee_features)}, scale={scale} m)")
            return carbon_predicted

        # ── Linear expression (ridge / lasso / linear / elastic_net) ────────
        self.ensure_gee_deployable()

        if hasattr(self.model.scaler, 'mean_') and self.model.metadata.get('scaled_features', True):
            scaler_mean = self.model.scaler.mean_.tolist()
            scaler_std = self.model.scaler.scale_.tolist()
            if len(scaler_mean) != len(expected_features) or len(scaler_std) != len(expected_features):
                raise ValueError(
                    f"Scaler feature count ({len(scaler_mean)}) does not match "
                    f"model feature count ({len(expected_features)}) for '{self.model_name}'. "
                    "Retrain/resave the model with matching scaler and feature_names metadata."
                )
            predictors_scaled = (
                predictors
                .subtract(ee.Image.constant(scaler_mean).rename(expected_features))
                .divide(ee.Image.constant(scaler_std).rename(expected_features))
            )
        else:
            predictors_scaled = predictors

        coef, intercept = self._get_model_coef_and_intercept()
        if len(coef) != len(expected_features):
            raise ValueError(
                f"Coefficient count ({len(coef)}) does not match feature count "
                f"({len(expected_features)}). Ensure the model metadata and scaler "
                "come from the same training run."
            )

        carbon_predicted = (
            predictors_scaled
            .multiply(ee.Image.constant(coef).rename(expected_features))
            .reduce(ee.Reducer.sum())
            .add(intercept)
            .max(ee.Image(0))
            .rename('carbon_estimated')
            .reproject(crs='EPSG:4326', scale=scale)
        )

        logger.info(
            f"Linear inference complete (features={len(coef)}, intercept={intercept:.4f}, scale={scale} m)"
        )

        return carbon_predicted
    
        # ------------------------------------------------------------------
        # 1. Load Sentinel-2 with pixel-level cloud masking
        #    - scene-level filter removes obviously cloudy acquisitions fast
        #    - SCL pixel mask then removes residual cloud / shadow pixels
        # ------------------------------------------------------------------
        start_date = f'{year}-{start_month:02d}-01'
        end_date   = f'{year}-{end_month:02d}-28'
    
        sen2_collection = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(roi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_threshold))
            .map(self._mask_s2_clouds)                # FIX A: pixel-level masking
        )
    
        collection_size = sen2_collection.size().getInfo()
        logger.info(f"Found {collection_size} Sentinel-2 images after cloud filter")
    
        if collection_size == 0:
            raise ValueError(
                "No Sentinel-2 images found. Try: "
                "(1) higher cloud_threshold, "
                "(2) wider date range, "
                "(3) different year."
            )
    
        # ------------------------------------------------------------------
        # 2. Build median composite and convert DN → surface reflectance
        # ------------------------------------------------------------------
        sen2 = sen2_collection.median().multiply(0.0001)
    
        # Select the 10 spectral bands used during training
        bands = ['B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B11', 'B12']
        sen2  = sen2.select(bands)
    
        # ------------------------------------------------------------------
        # 3. Compute all 9 spectral indices
        #    ORDER MUST MATCH data_preparation.calculate_indices() exactly:
        #    NDVI, NDWI, NDMI, NBR, NDRE, EVI, SAVI, BSI, brightness
        # ------------------------------------------------------------------
        ndvi = sen2.normalizedDifference(['B8',  'B4' ]).rename('NDVI')
        ndwi = sen2.normalizedDifference(['B3',  'B8' ]).rename('NDWI')
        ndmi = sen2.normalizedDifference(['B8',  'B11']).rename('NDMI')
    
        # FIX B: these four were missing in the original inference code
        nbr  = sen2.normalizedDifference(['B8',  'B12']).rename('NBR')
        ndre = sen2.normalizedDifference(['B8A', 'B5' ]).rename('NDRE')
    
        evi = sen2.expression(
            '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))',
            {'NIR': sen2.select('B8'), 'RED': sen2.select('B4'), 'BLUE': sen2.select('B2')}
        ).rename('EVI')
    
        savi = sen2.expression(
            '((NIR - RED) / (NIR + RED + 0.5)) * 1.5',
            {'NIR': sen2.select('B8'), 'RED': sen2.select('B4')}
        ).rename('SAVI')
    
        bsi = sen2.expression(
            '((SWIR1 + RED) - (NIR + BLUE)) / ((SWIR1 + RED) + (NIR + BLUE))',
            {
                'SWIR1': sen2.select('B11'),
                'RED':   sen2.select('B4'),
                'NIR':   sen2.select('B8'),
                'BLUE':  sen2.select('B2'),
            }
        ).rename('BSI')
    
        brightness = (sen2.select(['B2', 'B3', 'B4'])
                        .reduce(ee.Reducer.mean())
                        .rename('brightness'))
    
        # Stack into a 19-band image — same column order as feature_names in training
        # feature_names = [B2..B12 (10)] + [NDVI NDWI NDMI NBR NDRE EVI SAVI BSI brightness (9)]
        # FIX C: no 'constant=1' prepended — it was misaligning coef_ positions
        predictors = sen2.addBands([ndvi, ndwi, ndmi, nbr, ndre, evi, savi, bsi, brightness])

        self.ensure_gee_deployable()
        expected_features = self._get_expected_features()
        predictors = self._build_predictors(
            roi=roi,
            year=year,
            start_month=start_month,
            end_month=end_month,
            cloud_threshold=cloud_threshold,
        )
    
        # ------------------------------------------------------------------
        # 4. Standardise GEE pixels using the training scaler
        #    model.coef_ is in SCALED feature space — raw pixel values must be
        #    transformed with the same mean_ / scale_ before multiplication.
        #
        #    y = coef_ @ X_scaled + intercept_
        #      = coef_ @ ((X - mean) / std) + intercept_
        #
        #    We apply (X - mean) / std to the image, then dot with coef_.
        # ------------------------------------------------------------------
        if hasattr(self.model.scaler, 'mean_') and self.model.metadata.get('scaled_features', True):
            scaler_mean = self.model.scaler.mean_.tolist()
            scaler_std = self.model.scaler.scale_.tolist()
            if len(scaler_mean) != len(expected_features) or len(scaler_std) != len(expected_features):
                raise ValueError(
                    f"Scaler feature count ({len(scaler_mean)}) does not match "
                    f"model feature count ({len(expected_features)}) for '{self.model_name}'. "
                    "Retrain/resave the model with matching scaler and feature_names metadata."
                )

            mean_img = ee.Image.constant(scaler_mean).rename(expected_features)
            std_img  = ee.Image.constant(scaler_std).rename(expected_features)
    
            # Subtract per-feature mean, divide by per-feature std
            predictors_scaled = predictors.subtract(mean_img).divide(std_img)
            logger.info("✓ StandardScaler (mean/std) applied to GEE image")
        else:
            # Model was trained without scaling — use raw features directly
            predictors_scaled = predictors
            logger.info("Scaler not fitted — using raw reflectance values")
    
        # ------------------------------------------------------------------
        # 5. Retrieve coef_ and intercept_ (surrogate created if non-linear)
        # ------------------------------------------------------------------
        coef, intercept = self._get_model_coef_and_intercept()
    
        n_coef     = len(coef)
        n_features = len(expected_features)
        if n_coef != n_features:
            raise ValueError(
                f"Coefficient count ({n_coef}) does not match "
                f"feature count ({n_features}). "
                "Ensure predict_for_region computes the same features as training."
            )
    
        # ------------------------------------------------------------------
        # 6. Apply linear model:  carbon = sum(coef_i * X_i) + intercept
        #    - multiply each band by its coefficient
        #    - sum across bands
        #    - add scalar intercept
        # ------------------------------------------------------------------
        carbon_predicted = (
            predictors_scaled
            .multiply(ee.Image.constant(coef).rename(expected_features))
            .reduce(ee.Reducer.sum())
            .add(intercept)                         # FIX D: intercept added cleanly
            .rename('carbon_estimated')
            .reproject(crs='EPSG:4326', scale=scale)  # FIX E: match training scale
        )
    
        logger.info(
            f"✓ Inference complete "
            f"(features={n_coef}, intercept={intercept:.4f}, scale={scale} m)"
        )
    
        return carbon_predicted
    
    def predict_for_region_sampled(self,
                                   roi: ee.Geometry,
                                   year: int,
                                   start_month: int,
                                   end_month: int,
                                   cloud_threshold: int = 50,
                                   scale: int = 250,
                                   n_samples: int = 2000) -> dict:
        """
        Server-side inference for non-GEE-deployable models (RF, GB, etc.).

        Samples `n_samples` pixels from the feature stack via GEE, runs
        sklearn model.predict() locally, and returns summary statistics.
        No tile URL is produced.

        Returns dict with keys: mean, std, min, max, n_pixels, unit
        """
        import pandas as pd

        logger.info(
            f"[sampled] Server-side inference for {year}-{start_month:02d} to {year}-{end_month:02d} "
            f"at scale={scale}m, n_samples={n_samples}"
        )

        predictors = self._build_predictors(
            roi=roi,
            year=year,
            start_month=start_month,
            end_month=end_month,
            cloud_threshold=cloud_threshold,
        )

        sample_fc = predictors.sample(
            region=roi,
            scale=scale,
            numPixels=n_samples,
            seed=42,
            geometries=False,
        )

        features_list = sample_fc.getInfo().get("features", [])
        if not features_list:
            raise ValueError(
                "GEE returned 0 sampled pixels for this AOI. "
                "Try a larger region, higher cloud threshold, or different year."
            )

        expected_features = self._get_expected_features()
        rows = [f["properties"] for f in features_list]
        df = pd.DataFrame(rows)[expected_features]
        df = df.dropna()

        if len(df) == 0:
            raise ValueError("All sampled pixels have null values after dropping NaN.")

        X = df.values
        if hasattr(self.model.scaler, 'mean_') and self.model.metadata.get('scaled_features', True):
            X = self.model.scaler.transform(X)

        predictions = self.model.model.predict(X)
        predictions = np.clip(predictions, 0, None)

        logger.info(
            f"[sampled] {len(predictions)} pixels, mean={predictions.mean():.2f}, "
            f"std={predictions.std():.2f}"
        )

        return {
            "mean": float(predictions.mean()),
            "std": float(predictions.std()),
            "min": float(predictions.min()),
            "max": float(predictions.max()),
            "n_pixels": int(len(predictions)),
        }

    def _get_model_coefficients_for_gee(self) -> list:
        """
        Back-transform coefficients from scaled to original feature space.
        
        Model: y = coef @ X_scaled + intercept
            X_scaled = (X - mean) / std
        
        So in original space:
            y = (coef / std) @ X - (coef / std) @ mean + intercept
        """
        if not (hasattr(self.model, 'coef_') and hasattr(self.model, 'intercept_')):
            logger.warning("Non-linear model, attempting surrogate...")
            return self._create_linear_surrogate()
        
        coef_scaled = self.model.coef_.copy()
        intercept   = float(self.model.intercept_)
        
        # Jika scaler sudah di-fit, transform balik ke original space
        if (hasattr(self.scaler, 'mean_') and 
                self.metadata.get('scaled_features', True)):
            
            mean = self.scaler.mean_          # shape: (n_features,)
            std  = self.scaler.scale_         # shape: (n_features,)
            
            # Koefisien di original space
            coef_orig      = coef_scaled / std
            intercept_orig = intercept - float(np.dot(coef_scaled / std, mean))
            
            # Susun: [intercept, coef_B2, coef_B3, ..., coef_brightness]
            # Ini harus match dengan urutan band di GEE predictor image
            coefficients = [intercept_orig] + coef_orig.tolist()
        else:
            coefficients = [intercept] + coef_scaled.tolist()
        
        return coefficients
        
    def _create_linear_surrogate(self) -> list:
        """
        Create a linear surrogate model for non-linear models.
        This is used for GEE deployment when the original model is not linear.
        """
        from sklearn.linear_model import Ridge
        from sklearn.metrics import r2_score, mean_squared_error

        training_info = self.model.metadata.get("training_info", {}) or {}

        X_sample = training_info.get("X_sample")
        y_sample = training_info.get("y_sample")

        if X_sample is None or y_sample is None:
            raise ValueError(
                f"Model '{self.model_name}' with algorithm '{self.model.algorithm}' "
                "cannot be deployed directly to GEE because it is non-linear and "
                "no surrogate training samples were saved in metadata. "
                "Retrain/resave the model with metadata['training_info']['X_sample'] "
                "and metadata['training_info']['y_sample'], or use a linear model "
                "(linear/ridge/lasso/elasticnet)."
            )

        X_sample = np.array(X_sample)
        y_sample = np.array(y_sample)

        if X_sample.ndim != 2:
            raise ValueError("Invalid X_sample shape in model metadata")
        if y_sample.ndim != 1:
            y_sample = y_sample.reshape(-1)

        if len(X_sample) != len(y_sample):
            raise ValueError("X_sample and y_sample length mismatch in model metadata")

        logger.info("Training linear surrogate model...")

        # Prediksi model asli
        y_pred_original = self.model.model.predict(X_sample)

        # Latih surrogate linear
        surrogate = Ridge(alpha=1.0)
        surrogate.fit(X_sample, y_pred_original)

        coefficients = surrogate.coef_.tolist()
        intercept = float(surrogate.intercept_)

        y_pred_surrogate = surrogate.predict(X_sample)
        r2 = r2_score(y_pred_original, y_pred_surrogate)
        rmse = float(np.sqrt(mean_squared_error(y_pred_original, y_pred_surrogate)))

        logger.info("✓ Linear surrogate created")
        logger.info(f"  R² (surrogate vs original): {r2:.4f}")
        logger.info(f"  RMSE (surrogate vs original): {rmse:.2f} Mg/ha")

        if r2 < 0.8:
            logger.warning(
                f"Surrogate quality is low (R²={r2:.4f}). "
                "Consider using a linear model for more stable GEE deployment."
            )

        return coefficients, intercept
    
    def get_model_info(self) -> Dict:
        """Get information about the loaded model"""
        return self.model.get_info()
