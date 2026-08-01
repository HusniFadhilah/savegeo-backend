"""
carbon_inference_local.py — non-GEE inference.

Non-GEE analogue of inference/carbon_inference.py::CarbonInferenceEngine.
Features come from providers/stac_provider.py or providers/local_raster_provider.py
instead of ee.Image; otherwise reuses ModelRegistry/CarbonEstimationModel.load()
unchanged.

No live tile-serving Flask route is added here — predict_raster() writes a
GeoTIFF to disk. A future `/api/analyze/carbon-local` endpoint is a
documented follow-up only (see plan), per "don't touch production routes
without need."
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

from app.inference.model_registry import ModelRegistry
from app.inference.carbon_model import CarbonEstimationModel
from app.providers.feature_engineering_non_gee import build_feature_stack
from app.providers.local_raster_provider import GridSpec

logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]


class LocalCarbonInferenceEngine:
    """Non-GEE counterpart to CarbonInferenceEngine. Only usable with models
    whose metadata has provider in {"non_gee_stac", "local_raster"} and
    gee_deployable=False — feature building here never touches ee.Image.
    """

    def __init__(self, model_name: Optional[str] = None, model_path: Optional[str] = None):
        self.registry = ModelRegistry()
        if model_name is None:
            model_name = self.registry.registry.get("default")
            if model_name is None:
                raise ValueError("No default model found. Train a model first.")
        self.model_name = model_name
        if model_path is None:
            model_path = self.registry.get_model_path(model_name)
        self.model = CarbonEstimationModel.load(model_path)

    def _expected_features(self) -> list:
        features = self.model.feature_names or self.model.metadata.get("feature_names") or []
        if not features:
            raise ValueError(f"Model '{self.model_name}' has no feature_names in metadata")
        return list(features)

    def _feature_stack_key(self) -> str:
        key = self.model.metadata.get("feature_stack_key") or self.model.metadata.get("feature_stack")
        if not key:
            raise ValueError(f"Model '{self.model_name}' has no feature_stack/feature_stack_key in metadata")
        return key

    def _build_stack(self, bbox_lonlat: BBox, start_date: str, end_date: str, scale: float, provider: str):
        needs_dem_lc = self._feature_stack_key() == "standard_s2_non_gee_dem_landcover"
        if provider == "stac_pc":
            from providers.stac_provider import build_full_stack
            return build_full_stack(
                bbox_lonlat, start_date, end_date, resolution_m=scale,
                include_s1=False, include_dem=needs_dem_lc, include_landcover=needs_dem_lc,
            )
        raise ValueError(f"Unsupported provider '{provider}' for inference (only 'stac_pc' implemented)")

    def predict_for_region_sampled(
        self,
        bbox_lonlat: BBox,
        start_date: str,
        end_date: str,
        scale: float = 20,
        n_samples: int = 2000,
        provider: str = "stac_pc",
        seed: int = 42,
    ) -> Dict:
        """Non-GEE analogue of CarbonInferenceEngine.predict_for_region_sampled().
        Same return shape: {mean, std, min, max, n_pixels, unit}."""
        stack = self._build_stack(bbox_lonlat, start_date, end_date, scale, provider)
        feature_array, feature_names = build_feature_stack(stack, self._feature_stack_key())

        expected = self._expected_features()
        name_to_idx = {n: i for i, n in enumerate(feature_names)}
        missing = [f for f in expected if f not in name_to_idx]
        if missing:
            raise ValueError(f"Model expects features {missing} not present in built stack {feature_names}")
        ordered = np.stack([feature_array[name_to_idx[f]] for f in expected], axis=0)

        valid_mask = ~np.any(np.isnan(ordered), axis=0)
        valid_rows, valid_cols = np.where(valid_mask)
        if len(valid_rows) == 0:
            raise ValueError("predict_for_region_sampled: 0 valid pixels in built feature stack")

        rng = np.random.default_rng(seed)
        n_take = min(n_samples, len(valid_rows))
        idx = rng.choice(len(valid_rows), size=n_take, replace=False)
        rows, cols = valid_rows[idx], valid_cols[idx]

        X = ordered[:, rows, cols].T  # (n_take, n_features)
        if self.model.scaler is not None:
            X = self.model.scaler.transform(X)
        preds = self.model.model.predict(X)
        preds = np.clip(preds, 0, None)

        return {
            "mean": float(np.mean(preds)),
            "std": float(np.std(preds)),
            "min": float(np.min(preds)),
            "max": float(np.max(preds)),
            "n_pixels": int(n_take),
            "unit": self.model.metadata.get("target_unit", "Mg C/ha"),
        }

    def predict_raster(
        self,
        bbox_lonlat: BBox,
        start_date: str,
        end_date: str,
        output_path: Path,
        scale: float = 20,
        provider: str = "stac_pc",
        window_size_px: int = 512,
    ) -> Path:
        """Full-raster windowed prediction -> tiled GeoTIFF on disk.

        Builds the full-AOI feature stack once (already grid-aligned), then
        predicts in window_size_px x window_size_px tiles to bound memory,
        writing directly to a COG-friendly GeoTIFF.
        """
        import rasterio
        from rasterio.windows import Window

        stack = self._build_stack(bbox_lonlat, start_date, end_date, scale, provider)
        feature_array, feature_names = build_feature_stack(stack, self._feature_stack_key())
        expected = self._expected_features()
        name_to_idx = {n: i for i, n in enumerate(feature_names)}
        missing = [f for f in expected if f not in name_to_idx]
        if missing:
            raise ValueError(f"Model expects features {missing} not present in built stack {feature_names}")
        ordered = np.stack([feature_array[name_to_idx[f]] for f in expected], axis=0)

        h, w = stack.shape
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        profile = dict(
            driver="GTiff", height=h, width=w, count=1, dtype="float32",
            crs=stack.crs, transform=stack.transform, nodata=np.nan,
            tiled=True, blockxsize=256, blockysize=256, compress="deflate",
        )
        with rasterio.open(output_path, "w", **profile) as dst:
            for row0 in range(0, h, window_size_px):
                for col0 in range(0, w, window_size_px):
                    row1 = min(row0 + window_size_px, h)
                    col1 = min(col0 + window_size_px, w)
                    window = Window(col0, row0, col1 - col0, row1 - row0)

                    tile = ordered[:, row0:row1, col0:col1]
                    tile_h, tile_w = tile.shape[1], tile.shape[2]
                    flat = tile.reshape(len(expected), -1).T  # (pixels, n_features)
                    valid = ~np.any(np.isnan(flat), axis=1)

                    out_tile = np.full(flat.shape[0], np.nan, dtype=np.float32)
                    if valid.any():
                        X = flat[valid]
                        if self.model.scaler is not None:
                            X = self.model.scaler.transform(X)
                        preds = np.clip(self.model.model.predict(X), 0, None)
                        out_tile[valid] = preds

                    dst.write(out_tile.reshape(tile_h, tile_w).astype(np.float32), 1, window=window)

        logger.info(f"predict_raster: wrote {output_path} ({h}x{w})")
        return output_path


if __name__ == "__main__":
    print(
        "carbon_inference_local.py has no standalone smoke test — it requires a trained "
        "non-GEE model in saved_models/ (see train_carbon_non_gee.py). Run the end-to-end "
        "smoke test via train_carbon_non_gee.py first, then exercise this module against "
        "the resulting model."
    )
