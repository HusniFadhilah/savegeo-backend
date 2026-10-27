"""Reference maps and executable BGB/SOC models with explicit pool/unit semantics."""
import math
from datetime import UTC, datetime

import ee

from app.registries.carbon_dataset_registry import get_dataset_meta, load_carbon_reference_ee, soilgrids_stock_image
from app.registries.carbon_pool_models import get_builtin_carbon_model
from app.services import config_service
from app.services.gee_common import AnalysisError, geojson_to_ee_geometry


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


def _landsat_predictors(region, year):
    older = year < 2013
    if older:
        collection = ee.ImageCollection("LANDSAT/LT05/C02/T1_L2")
    else:
        collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        if year >= 2021:
            collection = collection.merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
    bands = ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7"] if older else ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]

    def prepare(image):
        mask = image.select("QA_PIXEL").bitwiseAnd(2 | 4 | 8 | 16 | 32).eq(0).And(image.select("QA_RADSAT").eq(0))
        return image.select(bands, ["blue", "green", "red", "nir", "swir1", "swir2"]).multiply(0.0000275).add(-0.2).updateMask(mask)

    optical = collection.filterBounds(region).filterDate(f"{year}-01-01", f"{year + 1}-01-01").map(prepare).median()
    ndvi = optical.normalizedDifference(["nir", "red"]).rename("ndvi")
    ndmi = optical.normalizedDifference(["nir", "swir1"]).rename("ndmi")
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30").select("DEM").mosaic().setDefaultProjection(ee.Projection("EPSG:4326").atScale(90))
    return optical.addBands([ndvi, ndmi, dem.rename("elevation"), ee.Terrain.slope(dem).rename("slope")])


def _random_forest(reference, roi, meta, model, analysis_year):
    # Bounded regional fitting keeps global-load requests separate from model calibration.
    region = roi.bounds(maxError=100).buffer(20000, maxError=100).bounds(maxError=100)
    area = region.area(maxError=100).divide(1e6).getInfo()
    if area > 100000:
        raise AnalysisError("Model RF BGB/SOC mendukung AOI regional hingga 100.000 km² termasuk buffer. Kecilkan AOI atau muat raster referensi langsung.", 422)
    reference_year = meta.get("selection_year") or (meta.get("year") if isinstance(meta.get("year"), int) else 2020)
    training_predictors = _landsat_predictors(region, reference_year)
    prediction_predictors = (
        training_predictors if analysis_year == reference_year else _landsat_predictors(region, analysis_year)
    )
    feature_names = ["blue", "green", "red", "nir", "swir1", "swir2", "ndvi", "ndmi", "elevation", "slope"]
    # Sample at no finer than 300 m, including 30 m modeled references. This
    # does not turn coarse soil/root training labels into field measurements.
    sample_scale = max(300, int(meta["resolution"]))
    lonlat = ee.Image.pixelLonLat()
    block = lonlat.select("longitude").divide(0.05).floor().add(lonlat.select("latitude").divide(0.05).floor().multiply(7)).abs().mod(5).rename("holdout")
    samples = reference.addBands(training_predictors).addBands(block).sample(region=region, scale=sample_scale, numPixels=1600, seed=42, geometries=False, tileScale=4)
    train = samples.filter(ee.Filter.neq("holdout", 0))
    valid = samples.filter(ee.Filter.eq("holdout", 0))
    counts = ee.Dictionary({"training": train.size(), "validation": valid.size()}).getInfo()
    if counts["training"] < 60 or counts["validation"] < 20:
        raise AnalysisError("Piksel referensi/kovariat valid terlalu sedikit untuk kalibrasi RF dan spatial holdout. Pilih AOI daratan lebih luas atau mode referensi.", 422)
    classifier = ee.Classifier.smileRandomForest(numberOfTrees=60, minLeafPopulation=5, bagFraction=0.7, seed=42).setOutputMode("REGRESSION").train(train, "agb", feature_names)
    predicted = prediction_predictors.classify(classifier, "agb").max(0).updateMask(reference.mask())
    checked = valid.classify(classifier, "prediction")
    average = ee.Number(checked.aggregate_mean("agb"))

    def residuals(feature):
        difference = ee.Number(feature.get("prediction")).subtract(feature.get("agb"))
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
    if model:
        if method == "saved_reference_ensemble":
            from app.services.carbon_saved_models import saved_model_image

            image, extras = saved_model_image(calculation_roi, model, meta, analysis_year=analysis_year)
            if reference is not None:
                image = image.updateMask(reference.mask())
        elif method == "soil_stock_integration":
            image = soilgrids_stock_image(meta["depth_cm"], ee)
        elif method == "ipcc_root_to_shoot":
            image = _ipcc_image(meta, model, calculation_roi)
        else:
            image, extras = _random_forest(reference, calculation_roi, meta, model, analysis_year)
    scale = max(int(meta["resolution"]), int(data.get("scale") or meta["resolution"]))
    vis = {"min": meta.get("vis_min", defaults["carbon_vis_min"]),
           "max": meta.get("vis_max", defaults["carbon_vis_max"]),
           "palette": meta.get("vis_palette", defaults["carbon_vis_palette"])}
    stats = None
    area_info = {"calculation_mode": "clipped_aoi" if clipped else "full_tiles", "total_carbon_tons": None,
                 "carbon_dioxide_equivalent_tons": None, "description": meta["description"]}
    coverage = None
    if not data.get("load_only"):
        display = image.clip(calculation_roi)
        pixel_ha = ee.Image.pixelArea().divide(10000).updateMask(display.mask()).rename("valid_area")
        bands = display.addBands(pixel_ha)
        stock = meta["unit"] == "Mg C/ha"
        if stock:
            bands = bands.addBands(display.multiply(pixel_ha).rename("carbon_total"))
        reducer = ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True).combine(ee.Reducer.minMax(), sharedInputs=True).combine(ee.Reducer.sum(), sharedInputs=True)
        reduced = bands.reduceRegion(reducer=reducer, geometry=calculation_roi, scale=scale,
                                     maxPixels=defaults["max_pixels"], tileScale=4)
        raw = reduced.set("aoi_area", calculation_roi.area(maxError=1).divide(10000)).getInfo()
        if raw.get("agb_mean") is None or not raw.get("valid_area_sum"):
            raise AnalysisError("AOI tidak memiliki piksel valid untuk dataset/model BGB atau SOC yang dipilih.", 422)
        stats = {"mean": raw["agb_mean"], "std_dev": raw.get("agb_stdDev"), "min": raw["agb_min"], "max": raw["agb_max"]}
        total = raw.get("carbon_total_sum") if stock else None
        coverage = min(100, raw["valid_area_sum"] / raw["aoi_area"] * 100) if raw["aoi_area"] else None
        area_info.update(calculation_area_ha=raw["aoi_area"], area_ha=raw["aoi_area"], valid_area_ha=raw["valid_area_sum"],
                         total_carbon_tons=total, carbon_dioxide_equivalent_tons=total * defaults["carbon_co2_factor"] if total is not None else None)
        if data.get("auto_vis") and stats["max"] > stats["min"]:
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
                             "calculation_mode": area_info["calculation_mode"], "model_version": model["version"] if model else None,
                             "reference_dataset": key, "reference_dataset_year": meta["year"], "analysis_year": analysis_year,
                             "requested_analysis_year": requested_analysis_year,
                             "analysis_year_partial": analysis_year == current_year,
                             "analysis_year_mode": "annual_covariate_inference" if annual_model else "static_reference",
                             "reference_year": source_year,
                             "target_pool": meta["target_pool"], "depth_cm": meta.get("depth_cm"), "source_period": meta.get("source_period"),
                             "load_only": bool(data.get("load_only")), "statistics_available": stats is not None,
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
    return result
