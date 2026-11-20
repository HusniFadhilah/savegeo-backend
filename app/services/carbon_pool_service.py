"""Reference maps and executable BGB/SOC models with explicit pool/unit semantics."""
import math
import logging
from datetime import UTC, datetime

import ee

from app.registries.carbon_dataset_registry import get_dataset_meta, load_carbon_reference_ee, soilgrids_stock_image
from app.registries.carbon_pool_models import get_builtin_carbon_model
from app.services import config_service
from app.services.gee_common import AnalysisError, geojson_to_ee_geometry

logger = logging.getLogger(__name__)


def _pool_density_statistics(raw, scale, native_scale):
    """Normalize the observed raster distribution to the chart response contract."""
    stats = {"mean": raw["agb_mean"], "std_dev": raw.get("agb_stdDev"),
             "min": raw["agb_min"], "max": raw["agb_max"],
             "percentiles": {"p25": raw.get("agb_p25"), "median": raw.get("agb_p50"), "p75": raw.get("agb_p75")}}
    histogram = raw.get("agb_histogram") or {}
    centers = histogram.get("bucketMeans", []) if isinstance(histogram, dict) else []
    counts = histogram.get("histogram", []) if isinstance(histogram, dict) else []
    if centers and len(centers) == len(counts):
        pairs = [(float(center), float(count)) for center, count in zip(centers, counts, strict=True)
                 if isinstance(center, (int, float)) and isinstance(count, (int, float))
                 and math.isfinite(center) and math.isfinite(count) and count >= 0]
        pairs.sort()
        if pairs and sum(count for _, count in pairs) > 0:
            stats["histogram"] = {"bucket_means": [center for center, _ in pairs],
                                  "counts": [count for _, count in pairs],
                                  "pixel_count": round(sum(count for _, count in pairs)),
                                  "sampled": scale > native_scale, "scale_m": scale}
    return stats


def _sample_pool_distribution(image, roi, scale):
    """Bounded fallback from real pixels, never from a synthetic density curve."""
    import numpy as np

    sampled = image.select(["agb"]).sample(region=roi, scale=scale, numPixels=3000,
                                            seed=42, geometries=False, tileScale=8).getInfo() or {}
    values = [feature.get("properties", {}).get("agb") for feature in sampled.get("features", [])]
    values = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
    if not values:
        return {}
    counts, edges = np.histogram(values, bins=24)
    quartiles = np.percentile(values, [25, 50, 75])
    return {"histogram": {"bucket_means": ((edges[:-1] + edges[1:]) / 2).tolist(),
                          "counts": counts.tolist(), "pixel_count": len(values), "sampled": True, "scale_m": scale},
            "percentiles": dict(zip(("p25", "median", "p75"), quartiles.tolist(), strict=True))}


def _geometry(aoi):
    if aoi.get("geojson"):
        return geojson_to_ee_geometry(aoi["geojson"])
    try:
        coords = [float(aoi[k]) for k in ("west", "south", "east", "north")]
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisError("AOI harus berupa GeoJSON atau batas west/south/east/north.", 400) from exc
    if not all(math.isfinite(v) for v in coords) or not (-180 <= coords[0] < coords[2] <= 180 and -90 <= coords[1] < coords[3] <= 90):
        raise AnalysisError("Batas AOI tidak valid.", 400)
    return ee.Geometry.Rectangle(coords)


def _landsat_predictors(region, year, fill_gaps=False):
    # Changing mask bits, collection choice or compositing here requires bumping
    # carbon_annual_store.ANNUAL_ALGORITHM_VERSION.
    older = year < 2013
    bands = ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7"] if older else ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]

    def prepare(image):
        mask = image.select("QA_PIXEL").bitwiseAnd(2 | 4 | 8 | 16 | 32).eq(0).And(image.select("QA_RADSAT").eq(0))
        return image.select(bands, ["blue", "green", "red", "nir", "swir1", "swir2"]).multiply(0.0000275).add(-0.2).updateMask(mask)

    legacy_sensor = year < 2013

    def collection_for_period(start_year, end_year):
        if legacy_sensor:
            # Landsat 5 and 7 bridge regional acquisition gaps before 2013.
            collection = ee.ImageCollection("LANDSAT/LT05/C02/T1_L2").merge(
                ee.ImageCollection("LANDSAT/LE07/C02/T1_L2")
            )
        else:
            collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
            if end_year > 2021:
                collection = collection.merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
        return collection.filterBounds(region).filterDate(f"{start_year}-01-01", f"{end_year}-01-01")

    optical_collection = collection_for_period(year, year + 1)
    optical = optical_collection.map(prepare).median()
    if fill_gaps:
        # Keep the target year's valid observations and use adjacent-year
        # medians only where annual cloud masking left no pixel. Tropical
        # AOIs often have holes in a single-year Landsat composite.
        start_year = max(1984, year - 1)
        end_year = min(datetime.now(UTC).year + 1, year + 2)
        fallback = collection_for_period(start_year, end_year).map(prepare).median()
        optical = optical.unmask(fallback)
    ndvi = optical.normalizedDifference(["nir", "red"]).rename("ndvi")
    ndmi = optical.normalizedDifference(["nir", "swir1"]).rename("ndmi")
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30").select("DEM").mosaic().setDefaultProjection(ee.Projection("EPSG:4326").atScale(90))
    return optical.addBands([ndvi, ndmi, dem.rename("elevation"), ee.Terrain.slope(dem).rename("slope")])


def _detritus_prediction_mask(roi, analysis_year):
    """Limit detritus predictions to observed tropical tree cover, not reference sample pixels."""
    year = max(2015, int(analysis_year))
    labels = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(roi)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
    )
    if not labels.size().getInfo():
        raise AnalysisError(f"Dynamic World tahun {year} belum tersedia untuk memetakan tutupan pohon.", 404)
    tree_cover = labels.select("label").mode().eq(1)
    mangrove = ee.ImageCollection("LANDSAT/MANGROVE_FORESTS").mosaic().select("1").unmask(0).eq(1)
    tropical = ee.Image.pixelLonLat().select("latitude").abs().lte(23.5)
    return tree_cover.And(mangrove.Not()).And(tropical)


def _training_sample_plan(is_detritus, area, sample_count):
    use_land_geometry = bool(is_detritus and area > 100000)
    return (max(sample_count, 10000) if use_land_geometry else sample_count), use_land_geometry


def _random_forest(reference, roi, meta, model, analysis_year, *, allow_large=False, cached_model_info=None):
    # Bounded regional fitting keeps global-load requests separate from model calibration.
    region = roi.bounds(maxError=100).buffer(20000, maxError=100).bounds(maxError=100)
    area = region.area(maxError=100).divide(1e6).getInfo()
    if area > 100000 and not allow_large:
        raise AnalysisError("Model RF BGB/SOC mendukung AOI regional hingga 100.000 km² termasuk buffer. Kecilkan AOI atau muat raster referensi langsung.", 422)
    reference_year = meta.get("selection_year") or (meta.get("year") if isinstance(meta.get("year"), int) else 2020)
    is_detritus = meta.get("target_pool") in {"litter_carbon", "deadwood_carbon"}
    training_predictors = _landsat_predictors(region, reference_year, fill_gaps=is_detritus)
    prediction_predictors = (
        training_predictors if analysis_year == reference_year else _landsat_predictors(region, analysis_year, fill_gaps=is_detritus)
    )
    feature_names = ["blue", "green", "red", "nir", "swir1", "swir2", "ndvi", "ndmi", "elevation", "slope"]
    # Sample at no finer than 300 m, including 30 m modeled references. This
    # does not turn coarse soil/root training labels into field measurements.
    sample_scale = max(300, int(meta["resolution"]))
    lonlat = ee.Image.pixelLonLat()
    block = lonlat.select("longitude").divide(0.05).floor().add(lonlat.select("latitude").divide(0.05).floor().multiply(7)).abs().mod(5).rename("holdout")
    sample_count = max(1600, min(20000, int(model.get("metadata_json", {}).get("rf_samples", 1600))))
    # On island/national bounding boxes most candidate pixels are ocean or
    # outside the forest reference mask. Sample the selected land geometry and
    # oversample candidates, while preserving the independent spatial holdout.
    sample_count, use_land_geometry = _training_sample_plan(is_detritus, area, sample_count)
    sample_region = roi if use_land_geometry else region
    samples = reference.addBands(training_predictors).addBands(block).sample(region=sample_region, scale=sample_scale, numPixels=sample_count, seed=42, geometries=False, tileScale=4)
    train = samples.filter(ee.Filter.neq("holdout", 0))
    valid = samples.filter(ee.Filter.eq("holdout", 0))
    cached_metrics = (cached_model_info or {}).get("validation_metrics")
    if cached_metrics:
        counts = {"training": int(cached_model_info.get("n_samples", 0)),
                  "validation": int(cached_model_info.get("validation_samples", 0))}
    else:
        counts = ee.Dictionary({"training": train.size(), "validation": valid.size()}).getInfo()
        if counts["training"] < 60 or counts["validation"] < 20:
            raise AnalysisError("Piksel referensi/kovariat valid terlalu sedikit untuk kalibrasi RF dan spatial holdout. Pilih AOI daratan lebih luas atau mode referensi.", 422)
    classifier = ee.Classifier.smileRandomForest(numberOfTrees=60, minLeafPopulation=5, bagFraction=0.7, seed=42).setOutputMode("REGRESSION").train(train, "agb", feature_names)
    predicted = prediction_predictors.classify(classifier, "agb").max(0)
    predicted = predicted.updateMask(
        _detritus_prediction_mask(roi, analysis_year) if is_detritus else reference.mask()
    )
    if cached_metrics:
        metrics = cached_metrics
    else:
        checked = valid.classify(classifier, "prediction")
        average = ee.Number(checked.aggregate_mean("agb"))

        def residuals(feature):
            difference = ee.Number(feature.get("prediction")).subtract(ee.Number(feature.get("agb")))
            return feature.set({"squared_error": difference.pow(2), "absolute_error": difference.abs(), "bias": difference,
                                "squared_deviation": ee.Number(feature.get("agb")).subtract(average).pow(2)})

        checked = checked.map(residuals)
        mse = ee.Number(checked.aggregate_mean("squared_error"))
        variance = ee.Number(checked.aggregate_mean("squared_deviation"))
        metrics = ee.Dictionary({"rmse": mse.sqrt(), "mae": checked.aggregate_mean("absolute_error"), "bias": checked.aggregate_mean("bias"),
                                 "r2": ee.Algorithms.If(variance.gt(0), ee.Number(1).subtract(mse.divide(variance)), None)}).getInfo()
    return predicted, {"n_samples": counts["training"], "validation_samples": counts["validation"],
                       "validation_metrics": metrics, "validation_type": "spatial_reference_holdout",
                       "sample_scale_m": sample_scale, "feature_names": feature_names,
                       "reference_year": reference_year, "covariate_year": analysis_year,
                       "training_area_km2": float(area), "training_domain": "selected_aoi_buffer",
                       "training_sample_scale_m": sample_scale, "training_sample_count": sample_count,
                       "large_aoi_regional_fit": bool(area > 100000),
                       "validation_note": "Metrik mengukur kesesuaian dengan raster referensi pada blok spasial terpisah; pemetaan tahun lain adalah transfer kovariat, belum divalidasi dengan data lapangan tahun tersebut."}


def _ipcc_image(meta, model, roi):
    # This preset applies to Asia's natural tropical forests, not to arbitrary AOIs.
    domain = ee.Geometry.Rectangle([55, -12, 180, 25], proj="EPSG:4326", geodesic=False)
    if not domain.contains(roi.bounds(maxError=100), maxError=100).getInfo():
        raise AnalysisError("Preset IPCC ini khusus hutan tropis Asia. Pilih RF atau dataset referensi untuk AOI lain.", 422)
    agb = load_carbon_reference_ee("SPAWN", 2010)
    info = model["metadata_json"]
    mass = agb.divide(info["carbon_fractions"]["agb"])
    low, high = info["ratios"]
    ratio = ee.Image.constant(low).where(mass.gt(info["biomass_threshold"]), high)
    forest = ee.Image("UMD/hansen/global_forest_change_2023_v1_11")
    mask = forest.select("treecover2000").gte(10).And(forest.select("lossyear").eq(0).Or(forest.select("lossyear").gt(10)))
    return mass.multiply(ratio).multiply(info["carbon_fractions"]["bgb"]).updateMask(mask).rename("agb")


def summary_scale(native_scale, bounds_area_m2, max_pixels):
    """Bound a reference summary's pixel count; the displayed raster stays native."""
    budget = max(1, min(int(max_pixels), 1_000_000))
    return max(int(native_scale), math.ceil(math.sqrt(bounds_area_m2 / budget)))


def build_pool_export_image(db, data, roi):
    """Use the same pool model and reference semantics as the analysis map."""
    key = data["reference_dataset"]
    year = int(data.get("dataset_year") or data.get("year", 2020))
    meta = get_dataset_meta(key, year)
    reference = load_carbon_reference_ee(key, year)
    model = None if data.get("reference_only") else get_builtin_carbon_model(data.get("model_name"), db=db)
    if not model:
        return reference
    if key not in model["metadata_json"]["compatible_reference_datasets"]:
        raise AnalysisError("Model ekspor tidak kompatibel dengan dataset pool.", 422)
    method = model["metadata_json"].get("method")
    if method == "saved_reference_ensemble":
        from app.services.carbon_saved_models import saved_model_image

        image, _ = saved_model_image(roi, model, meta, analysis_year=int(data["year"]))
        if key not in {"TROPICAL_LITTER_REFERENCE", "TROPICAL_DEADWOOD_REFERENCE"}:
            image = image.updateMask(reference.mask())
        return image
    if method == "soil_stock_integration":
        return soilgrids_stock_image(meta["depth_cm"], ee)
    if method == "ipcc_root_to_shoot":
        return _ipcc_image(meta, model, roi)
    image, _ = _random_forest(reference, roi, meta, model, int(data["year"]),
                              allow_large=bool(data.get("simple_mode")))
    return image


def analyze_carbon_pool(db, data):
    key = data["reference_dataset"]
    year = int(data.get("dataset_year", 2020))
    meta = get_dataset_meta(key, year)
    roi = _geometry(data["aoi"])
    clipped = bool(data.get("clip_to_aoi", True))
    calculation_roi = roi if clipped else roi.bounds(maxError=1)
    defaults = config_service.get_analysis_defaults(db)
    model = get_builtin_carbon_model(data.get("model_name"), db=db) if not data.get("reference_only") and not data.get("load_only") else None
    from app.registries.carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY, CARBON_ARCGIS_REGISTRY

    external = key in CARBON_EXTERNAL_REGISTRY or key in CARBON_ARCGIS_REGISTRY
    if external and meta.get("available_years"):
        meta["year"] = min(meta["available_years"], key=lambda value: abs(value - year))
    reference = None if external else load_carbon_reference_ee(key, year)
    if model and key not in model["metadata_json"]["compatible_reference_datasets"]:
        raise AnalysisError("Model BGB/SOC tidak kompatibel dengan dataset/kedalaman yang dipilih.", 422)
    extras = {}
    image = reference
    method = model["metadata_json"].get("method") if model else None
    source_year = int(meta.get("selection_year") or meta.get("year") or year)
    requested_analysis_year = int(data.get("year", datetime.now(UTC).year))
    current_year = datetime.now(UTC).year
    if not 1984 <= requested_analysis_year <= current_year:
        raise AnalysisError(f"Tahun inferensi harus antara 1984-{current_year}.", 400)
    annual_model = bool(model and method not in {"soil_stock_integration", "ipcc_root_to_shoot"})
    analysis_year = requested_analysis_year if annual_model else source_year
    scale = max(int(meta["resolution"]), int(data.get("scale") or meta["resolution"]))
    simple_baseline = bool(data.get("simple_mode") and not model)
    if data.get("simple_mode"):
        bounds_area = calculation_roi.bounds(maxError=100).area(maxError=100).getInfo()
        scale = summary_scale(scale, bounds_area, defaults["max_pixels"])
    annual_identity = None
    saved_summary = None
    if annual_model and not data.get("load_only"):
        from app.services.carbon_annual_store import estimate_identity, load_summary

        annual_identity = estimate_identity(data, model, analysis_year, source_year, scale)
        saved_summary = load_summary(db, annual_identity[0])
    cached_model_info = saved_summary.get("model_info") if saved_summary else None
    if model:
        if method == "saved_reference_ensemble":
            from app.services.carbon_saved_models import saved_model_image

            image, extras = saved_model_image(calculation_roi, model, meta, analysis_year=analysis_year)
            if reference is not None and key not in {"TROPICAL_LITTER_REFERENCE", "TROPICAL_DEADWOOD_REFERENCE"}:
                image = image.updateMask(reference.mask())
        elif method == "soil_stock_integration":
            image = soilgrids_stock_image(meta["depth_cm"], ee)
        elif method == "ipcc_root_to_shoot":
            image = _ipcc_image(meta, model, calculation_roi)
        else:
            image, extras = _random_forest(reference, calculation_roi, meta, model, analysis_year,
                                           allow_large=bool(data.get("simple_mode")),
                                           cached_model_info=cached_model_info)
    vis = {"min": meta.get("vis_min", defaults["carbon_vis_min"]),
           "max": meta.get("vis_max", defaults["carbon_vis_max"]),
           "palette": meta.get("vis_palette", defaults["carbon_vis_palette"])}
    stats = None
    area_info = {"calculation_mode": "clipped_aoi" if clipped else "full_tiles", "total_carbon_tons": None,
                 "carbon_dioxide_equivalent_tons": None, "description": meta["description"]}
    coverage = None
    if saved_summary:
        stats = saved_summary["statistics"]
        area_info = saved_summary["area_info"]
        coverage = saved_summary["coverage"]
    elif not data.get("load_only"):
        display = image.clip(calculation_roi)
        pixel_ha = ee.Image.pixelArea().divide(10000).updateMask(display.mask()).rename("valid_area")
        bands = display.addBands(pixel_ha)
        stock = meta["unit"] == "Mg C/ha"
        if stock:
            bands = bands.addBands(display.multiply(pixel_ha).rename("carbon_total"))
        reducer = (ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True)
                   .combine(ee.Reducer.minMax(), sharedInputs=True).combine(ee.Reducer.sum(), sharedInputs=True)
                   .combine(ee.Reducer.percentile([25, 50, 75]), sharedInputs=True)
                   .combine(ee.Reducer.histogram(32, None, 10000), sharedInputs=True))
        reduced = bands.reduceRegion(reducer=reducer, geometry=calculation_roi, scale=scale,
                                     maxPixels=defaults["max_pixels"], tileScale=4)
        raw = reduced.set("aoi_area", calculation_roi.area(maxError=1).divide(10000)).getInfo()
        if raw.get("agb_mean") is None or not raw.get("valid_area_sum"):
            raise AnalysisError("AOI tidak memiliki piksel valid untuk dataset/model BGB atau SOC yang dipilih.", 422)
        stats = _pool_density_statistics(raw, scale, int(meta["resolution"]))
        if not stats.get("histogram"):
            try:
                distribution = _sample_pool_distribution(display, calculation_roi, scale)
                if distribution:
                    stats.update(distribution)
            except Exception:  # noqa: BLE001 - preserve valid totals if optional distribution sampling fails
                logger.warning("Pool distribution sample failed for %s", key, exc_info=True)
        total = raw.get("carbon_total_sum") if stock else None
        coverage = min(100, raw["valid_area_sum"] / raw["aoi_area"] * 100) if raw["aoi_area"] else None
        area_info.update(calculation_area_ha=raw["aoi_area"], area_ha=raw["aoi_area"], valid_area_ha=raw["valid_area_sum"],
                         total_carbon_tons=total, carbon_dioxide_equivalent_tons=total * defaults["carbon_co2_factor"] if total is not None else None)
    if data.get("auto_vis") and stats and stats["max"] > stats["min"]:
        vis.update(min=stats["min"], max=stats["max"])
    display = image.clip(calculation_roi)
    tile = display.getMapId(vis)["tile_fetcher"].url_format
    if external:
        if key in CARBON_ARCGIS_REGISTRY or key == "CHLORIS_AGB_STOCK":
            from app.services.carbon_service import get_direct_carbon_reference_layer

            reference_tile = get_direct_carbon_reference_layer(key, year, vis_min=vis["min"], vis_max=vis["max"])["tile_url"]
        else:
            reference_tile = f"/api/carbon/reference-tiles/{key}/{{z}}/{{x}}/{{y}}.png?year={meta['year']}&min={vis['min']}&max={vis['max']}"
    else:
        reference_tile = reference.clip(calculation_roi).getMapId(vis)["tile_fetcher"].url_format if model else tile
    provenance = {k: meta.get(k) for k in ("unit", "target_pool", "quantity", "depth_cm", "source_period", "product_version", "source_url", "attribution")}
    reference_info = {**provenance, "tile_url": reference_tile, "name": meta["name"], "full_name": meta["full_name"],
                      "year": meta["year"], "resolution": meta["resolution"], "description": meta["description"], "vis_params": vis}
    if not model:
        reference_info["statistics"] = stats
    estimated = {**provenance, "tile_url": tile if model else None, "statistics": stats, "vis_params": vis,
                 "inference_mode": "builtin_model" if model else "reference_only"}
    result = {"carbon_estimated": estimated, "carbon_reference": reference_info, "area_info": area_info,
              "model_info": {"model_name": model["name"] if model else "reference_only", "algorithm": model["algorithm"] if model else "direct_raster",
                             "display_mode": "model" if model else "reference_only", "scale": scale,
                             **({"map_resolution_m": 30, "reference_support_m": meta["resolution"]}
                                if key in {"TROPICAL_LITTER_REFERENCE", "TROPICAL_DEADWOOD_REFERENCE"} and model else {}),
                             "calculation_mode": area_info["calculation_mode"], "model_version": model["version"] if model else None,
                             "reference_dataset": key, "reference_dataset_year": meta["year"], "analysis_year": analysis_year,
                             "requested_analysis_year": requested_analysis_year,
                             "analysis_year_partial": analysis_year == current_year,
                             "analysis_year_mode": "annual_covariate_inference" if annual_model else "static_reference",
                             "reference_year": source_year,
                             "target_pool": meta["target_pool"], "depth_cm": meta.get("depth_cm"), "source_period": meta.get("source_period"),
                             "load_only": bool(data.get("load_only")), "statistics_available": stats is not None,
                             "large_aoi_baseline": simple_baseline,
                             "method_description": model["description"] if model else meta["description"], **extras},
              "data_quality": {"valid_pixel_pct": coverage, "gap_filled": None, "images_used": None,
                               "analysis_year_partial": analysis_year == current_year,
                               "coefficient_of_variation_pct": stats["std_dev"] / stats["mean"] * 100 if stats and stats["mean"] and stats["std_dev"] is not None else None,
                               "model_r2": None, "model_rmse": None}}
    if key == "ORNL_BGB":
        uncertainty = ee.Image(ee.ImageCollection(meta["gee_id"]).first()).select("bgb_uncertainty").clip(calculation_roi)
        uncertainty_vis = {"min": 0, "max": 30, "palette": ["ffffcc", "fd8d3c", "800026"]}
        result["carbon_uncertainty"] = {"tile_url": uncertainty.getMapId(uncertainty_vis)["tile_fetcher"].url_format,
                                        "unit": meta["unit"], "vis_params": uncertainty_vis, "uncertainty_type": "reference_standard_error",
                                        "description": "Standard error raster referensi NASA/ORNL; bukan interval kepercayaan model RF/IPCC."}
    if annual_identity:
        from app.services.carbon_annual_store import save_summary

        persisted = bool(saved_summary)
        if not saved_summary:
            persisted = save_summary(db, annual_identity, data, model, analysis_year, source_year,
                                     {"statistics": stats, "area_info": area_info, "coverage": coverage,
                                      "model_info": {key: extras.get(key) for key in (
                                          "n_samples", "validation_samples", "validation_metrics", "validation_type",
                                          "sample_scale_m", "training_area_km2", "training_domain",
                                          "training_sample_scale_m", "training_sample_count", "large_aoi_regional_fit",
                                          "reference_year", "covariate_year", "validation_note",
                                      ) if key in extras}})
        result["model_info"].update(
            annual_summary_source="database" if saved_summary else "computed",
            annual_summary_key=annual_identity[0],
            annual_summary_persisted=persisted,
            observed_through=annual_identity[2].isoformat(),
        )
    return result
