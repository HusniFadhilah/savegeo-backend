"""Carbon dataset discovery helpers — DB + registry lookups only, no GEE calls —
plus (appended below) the GEE-heavy carbon analysis route-body logic.

Discovery helpers ported from legacy `app.py::get_carbon_dataset_list` /
`_active_carbon_model_compatibility`. Analysis helpers ported from legacy
`app.py::calculate_carbon_summary_for_year` (lines ~1623-1679) and the
`/api/analyze/carbon*` route bodies (lines ~2765-3370).
"""
from __future__ import annotations

from app.services import config_service
import json
import logging
from datetime import datetime
from typing import Optional

import ee
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel
from app.inference.carbon_inference import CarbonInferenceEngine
from app.inference.carbon_inference_local import LocalCarbonInferenceEngine
from app.providers.external_carbon_provider import ExternalRasterProvider
from app.registries import model_compatibility
from app.repositories import dataset_repo
from app.registries.carbon_dataset_registry import (
    CARBON_ARCGIS_REGISTRY,
    CARBON_DATASET_REGISTRY,
    CARBON_EXTERNAL_REGISTRY,
    get_arcgis_carbon_meta,
    get_dataset_meta,
    get_external_carbon_meta,
    is_arcgis_carbon_dataset,
    is_external_carbon_dataset,
    list_dataset_keys,
    load_carbon_reference_ee,
    load_external_carbon_reference_ee,
)
from app.repositories.uploaded_model_repo import get_active_model_path
from app.services.arcgis_helpers import (
    _arcgis_carbon_reference_stats,
    _estimated_carbon_vis_params,
    _gee_visualize_params,
)
from app.services.gee_common import (
    AnalysisError,
    create_geometry_from_payload,
    geojson_to_ee_geometry,
    geometry_area_ha,
)

logger = logging.getLogger(__name__)


def active_carbon_model_compatibility(db: Session) -> dict[str, list[str]]:
    """Return active carbon model names grouped by compatible reference dataset key."""
    grouped: dict[str, list[str]] = {}
    models = db.query(UploadedModel).filter_by(model_type="carbon", is_active=True).all()
    for model in models:
        meta = model.metadata_json or {}
        meta = model_compatibility.backfill_legacy_metadata(meta, model.name)
        for dataset_key in model_compatibility.get_compatible_datasets(meta):
            grouped.setdefault(dataset_key, []).append(model.name)
    return grouped


def get_carbon_dataset_list(
    db: Session, provider: Optional[str] = None, require_model: bool = True
) -> list[dict]:
    """Return carbon reference datasets suitable for analysis dropdowns.

    Applies the `datasets` table admin-override layer (deactivate / rename /
    re-describe a dataset without a redeploy) - see app/repositories/dataset_repo.py.
    A dataset with no DB row behaves exactly as before this layer existed.
    """
    compatible_models = active_carbon_model_compatibility(db)
    overrides = dataset_repo.get_overrides_by_key(db, "carbon")
    ordered_keys = (
        list_dataset_keys()
        + list(CARBON_ARCGIS_REGISTRY.keys())
        + list(CARBON_EXTERNAL_REGISTRY.keys())
    )

    result = []
    seen = set()
    for key in ordered_keys:
        if key in seen:
            continue
        seen.add(key)

        model_names = compatible_models.get(key, [])
        if require_model and not model_names:
            continue

        meta = get_dataset_meta(key)
        meta = dataset_repo.apply_override(meta, overrides.get(key))
        if meta is None:
            continue  # admin deactivated this dataset

        provider_type = meta.get("provider_type")
        if not provider_type:
            provider_type = "gee_official" if key in CARBON_DATASET_REGISTRY else "external_raster"

        if provider is not None:
            if provider == "arcgis" and not provider_type.startswith("arcgis"):
                continue
            if provider != "arcgis" and provider != provider_type:
                continue

        result.append({
            "key": key,
            "name": meta.get("name"),
            "full_name": meta.get("full_name"),
            "module": "carbon",
            "provider_type": provider_type,
            "unit": meta.get("unit"),
            "target_pool": meta.get("target_pool"),
            "resolution": meta.get("resolution"),
            "year": meta.get("year"),
            "year_range": meta.get("year_range"),
            "description": meta.get("description"),
            "attribution": meta.get("attribution"),
            "limitations": meta.get("limitations", []),
            "compatible_model_count": len(model_names),
            "compatible_models": model_names,
            "training_capable": meta.get("training_capable", key in CARBON_DATASET_REGISTRY),
        })

    return result


# ─────────────────────────────────────────────
# GEE-heavy analysis helpers / route-body logic
# ─────────────────────────────────────────────

def calculate_carbon_summary_for_year(db, inference_engine, roi, year, start_month, end_month, cloud_threshold, carbon_scale):
    settings = get_settings()
    analysis_defaults = config_service.get_analysis_defaults(db)
    meta = inference_engine.model.metadata
    gee_algo_type = meta.get("gee_algorithm_type") or (
        "native_classifier" if meta.get("gee_classifier_path")
        else ("linear_expression" if meta.get("gee_deployable") else "server_side_only")
    )

    area_ha = geometry_area_ha(roi)
    co2_factor = analysis_defaults["carbon_co2_factor"]

    if gee_algo_type in ("linear_expression", "native_classifier"):
        carbon_image = inference_engine.predict_for_region(
            roi=roi, year=year,
            start_month=start_month, end_month=end_month,
            cloud_threshold=cloud_threshold,
        )
        stats = carbon_image.clip(roi).reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.stdDev(), "", True)
                .combine(ee.Reducer.min(), "", True)
                .combine(ee.Reducer.max(), "", True),
            geometry=roi,
            scale=carbon_scale,
            maxPixels=config_service.get_analysis_defaults(db)["max_pixels"],
            bestEffort=True,
            tileScale=4,
        ).getInfo()
        mean_carbon = stats.get("carbon_estimated_mean", 0) or 0
        std_dev = stats.get("carbon_estimated_stdDev", 0) or 0
        min_carbon = stats.get("carbon_estimated_min", 0) or 0
        max_carbon = stats.get("carbon_estimated_max", 0) or 0
    else:
        sampled = inference_engine.predict_for_region_sampled(
            roi=roi, year=year,
            start_month=start_month, end_month=end_month,
            cloud_threshold=cloud_threshold,
            scale=carbon_scale,
        )
        mean_carbon = sampled["mean"]
        std_dev = sampled["std"]
        min_carbon = sampled["min"]
        max_carbon = sampled["max"]

    total_carbon = mean_carbon * area_ha
    return {
        "year": int(year),
        "mean_density": round(mean_carbon, 2),
        "std_dev": round(std_dev, 2),
        "min": round(min_carbon, 2),
        "max": round(max_carbon, 2),
        "area_ha": round(area_ha, 2),
        "total_carbon_tons": round(total_carbon, 2),
        "carbon_dioxide_equivalent_tons": round(total_carbon * co2_factor, 2),
    }


def analyze_carbon(db: Session, data: dict) -> dict:
    if "aoi" not in data:
        raise AnalysisError("Missing required field: aoi", 400)

    settings = get_settings()

    year = int(data.get("year"))
    start_month = int(data.get("start_month"))
    end_month = int(data.get("end_month"))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    clip_to_aoi = data.get("clip_to_aoi", True)
    model_name = data.get("model_name")
    dataset_year = int(data.get("dataset_year", 2010))
    reference_dataset = data.get("reference_dataset", "WCMC")
    vis_min = int(data.get("vis_min", config_service.get_analysis_defaults(db)["carbon_vis_min"]))
    vis_max = int(data.get("vis_max", config_service.get_analysis_defaults(db)["carbon_vis_max"]))
    vis_palette = data.get("vis_palette", config_service.get_analysis_defaults(db)["carbon_vis_palette"])
    carbon_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["carbon_scale"]))
    match_reference_vis = bool(data.get("match_reference_vis", True))

    year_min, year_max = 2015, datetime.now().year
    if not (year_min <= year <= year_max):
        raise AnalysisError(f"Year harus antara {year_min}-{year_max}", 400)

    dataset_info = get_dataset_meta(reference_dataset, dataset_year)

    _is_arcgis_ref = is_arcgis_carbon_dataset(reference_dataset)
    _arcgis_ref_meta = get_arcgis_carbon_meta(reference_dataset) if _is_arcgis_ref else None
    _is_external_ref = is_external_carbon_dataset(reference_dataset)
    _external_ref_meta = get_external_carbon_meta(reference_dataset) if _is_external_ref else None

    if "geojson" in data["aoi"]:
        roi_original = geojson_to_ee_geometry(data["aoi"]["geojson"])
        has_geojson = True
    else:
        roi_original = ee.Geometry.Rectangle([
            data["aoi"]["west"], data["aoi"]["south"],
            data["aoi"]["east"], data["aoi"]["north"]
        ])
        has_geojson = False

    roi_for_filtering = roi_original
    if clip_to_aoi and has_geojson:
        roi_for_calculation = roi_original
    else:
        _b = roi_original.bounds().getInfo()["coordinates"][0]
        roi_for_calculation = ee.Geometry.Rectangle(_b[0] + _b[2])
    calculation_mode = "clipped_aoi" if (clip_to_aoi and has_geojson) else "full_tiles"

    _is_cloud_geotiff_ee_ref = (
        _is_external_ref
        and _external_ref_meta is not None
        and _external_ref_meta.get("ingestion_method") == "cloud_geotiff_ee"
    )
    if not _is_arcgis_ref and not _is_external_ref:
        carbon_reference = load_carbon_reference_ee(reference_dataset, dataset_year=dataset_year, roi=roi_for_calculation)
    elif _is_cloud_geotiff_ee_ref:
        try:
            carbon_reference = load_external_carbon_reference_ee(
                reference_dataset, dataset_year, roi_for_calculation
            )
        except ValueError as _ext_ee_err:
            raise AnalysisError(str(_ext_ee_err), 422, extra={"dataset": reference_dataset})
    else:
        carbon_reference = None

    # Cari model dari DB
    model_path = get_active_model_path(db, "carbon", model_name)
    inference_engine = CarbonInferenceEngine(model_name=model_name, model_path=model_path)
    model_info = inference_engine.get_model_info()

    # Validate model-dataset compatibility before any expensive GEE work.
    _db_model = db.query(UploadedModel).filter_by(
        name=inference_engine.model_name, is_active=True
    ).first()
    if _db_model and _db_model.metadata_json:
        _compat_meta = (
            json.loads(_db_model.metadata_json)
            if isinstance(_db_model.metadata_json, str)
            else _db_model.metadata_json
        ) or {}
    else:
        # Fall back to pkl metadata for legacy filesystem models.
        _compat_meta = dict(model_info)

    _compat_meta = model_compatibility.backfill_legacy_metadata(_compat_meta, inference_engine.model_name)

    # Non-GEE-trained models (STAC/local-raster feature stacks, e.g. interaction terms
    # like NDVI_x_elevation) cannot be built server-side in GEE — CarbonInferenceEngine
    # would fail deep inside tile construction with a confusing missing-feature error.
    # Reject early with a clear pointer to the correct endpoint instead.
    _model_provider = _compat_meta.get("provider")
    if _model_provider in ("non_gee_stac", "local_raster"):
        raise AnalysisError(
            f"Model '{inference_engine.model_name}' was trained via a non-GEE pipeline "
            f"(provider='{_model_provider}') and cannot be served through this GEE tile "
            "endpoint. Use POST /api/analyze/carbon-local with the same model_name instead.",
            400,
            extra={"provider": _model_provider, "correct_endpoint": "/api/analyze/carbon-local"},
        )

    model_compatibility.validate_model_dataset_compatibility(_compat_meta, reference_dataset, require_gee=False)

    is_gee_deployable = bool(_compat_meta.get("gee_deployable", False))
    co2_factor = config_service.get_analysis_defaults(db)["carbon_co2_factor"]
    vis_params = {"min": vis_min, "max": vis_max, "palette": vis_palette}

    # Reference tile — branched: ArcGIS proxy URL, external sampled stats, or GEE tile
    reference_tile_url = None
    ref_vis_params = vis_params
    arcgis_reference_stats = None
    external_reference_stats = None

    # Build AOI GeoJSON once (shared across branches)
    _aoi_geojson = data["aoi"].get("geojson") or {
        "type": "Polygon",
        "coordinates": [[
            [data["aoi"]["west"], data["aoi"]["south"]],
            [data["aoi"]["east"], data["aoi"]["south"]],
            [data["aoi"]["east"], data["aoi"]["north"]],
            [data["aoi"]["west"], data["aoi"]["north"]],
            [data["aoi"]["west"], data["aoi"]["south"]],
        ]]
    }

    if _is_arcgis_ref and _arcgis_ref_meta:
        # ArcGIS reference: tile proxy + computeHistograms for stats
        reference_tile_url = f"/api/arcgis/tiles/{reference_dataset}/{{z}}/{{y}}/{{x}}"
        ref_vis_params = {
            "min": _arcgis_ref_meta.get("vis_min", 0),
            "max": _arcgis_ref_meta.get("vis_max", 300),
            "palette": _arcgis_ref_meta.get("vis_palette", vis_palette),
        }
        try:
            arcgis_reference_stats = _arcgis_carbon_reference_stats(_arcgis_ref_meta, _aoi_geojson)
            ref_vis_params = arcgis_reference_stats.get("vis_params", ref_vis_params)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ArcGIS reference stats error: {e}")

    elif _is_cloud_geotiff_ee_ref and _external_ref_meta:
        ref_vis_params = {
            "min": _external_ref_meta.get("vis_min", 0),
            "max": _external_ref_meta.get("vis_max", 300),
            "palette": _external_ref_meta.get("vis_palette", vis_palette),
        }
        if carbon_reference is not None:
            try:
                reference_tile_url = (
                    carbon_reference.clip(roi_for_calculation)
                    .visualize(**_gee_visualize_params(ref_vis_params))
                    .getMapId()["tile_fetcher"].url_format
                )
            except Exception as _ctee_vis_err:  # noqa: BLE001
                logger.warning(
                    f"cloud_geotiff_ee tile URL failed for '{reference_dataset}': {_ctee_vis_err}"
                )

    elif _is_external_ref and _external_ref_meta:
        # External raster reference: sampled point statistics only.
        # If the registry defines gee_vis_id, also generate a GEE tile for visualization.
        ref_vis_params = {
            "min": _external_ref_meta.get("vis_min", 0),
            "max": _external_ref_meta.get("vis_max", 80),
            "palette": _external_ref_meta.get("vis_palette", vis_palette),
        }
        _gee_vis_id = _external_ref_meta.get("gee_vis_id")
        _gee_vis_band = _external_ref_meta.get("gee_vis_band", "agb")
        if _gee_vis_id:
            try:
                _gee_vis_img = ee.Image(_gee_vis_id).select(_gee_vis_band).clip(roi_for_calculation)
                _transform = _external_ref_meta.get("transform", "none")
                if _transform.startswith("multiply_"):
                    try:
                        _factor = float(_transform.split("_", 1)[1])
                        _gee_vis_img = _gee_vis_img.multiply(_factor)
                    except ValueError:
                        pass
                _gee_vis_img = _gee_vis_img.rename("agb")
                reference_tile_url = (
                    _gee_vis_img.visualize(**_gee_visualize_params(ref_vis_params))
                    .getMapId()["tile_fetcher"].url_format
                )
            except Exception as _gee_vis_err:  # noqa: BLE001
                logger.warning(
                    f"GEE tile visualization failed for external ref '{reference_dataset}': {_gee_vis_err}"
                )
        try:
            _ext_provider = ExternalRasterProvider()
            external_reference_stats = _ext_provider.get_stats_for_aoi(
                _external_ref_meta, _aoi_geojson
            )
        except NotImplementedError as nie:
            logger.info(f"External raster stats not available for '{reference_dataset}': {nie}")
            external_reference_stats = {
                "note": str(nie),
                "unit": _external_ref_meta.get("unit", ""),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"External raster stats error for '{reference_dataset}': {e}")
            external_reference_stats = None

    else:
        # GEE reference (original code — unchanged)
        try:
            reference_display = carbon_reference.clip(roi_for_calculation)
            ref_stats_raw = reference_display.reduceRegion(
                reducer=ee.Reducer.percentile([2, 98]),
                geometry=roi_for_calculation, scale=carbon_scale,
                maxPixels=config_service.get_analysis_defaults(db)["max_pixels"], bestEffort=True
            ).getInfo()
            ref_min = ref_stats_raw.get("agb_p2", vis_min) or vis_min
            ref_max = ref_stats_raw.get("agb_p98", vis_max) or vis_max
            ref_vis_params = {"min": ref_min, "max": ref_max, "palette": vis_palette}
            reference_tile_url = reference_display.visualize(**_gee_visualize_params(ref_vis_params)).getMapId()["tile_fetcher"].url_format
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Reference tile error: {e}")

    original_area = geometry_area_ha(roi_original)
    calculation_area = geometry_area_ha(roi_for_calculation)
    filtering_area = geometry_area_ha(roi_for_filtering)

    if is_gee_deployable:
        # ── GEE tile-map inference (linear/ridge/lasso) ──────────────────
        carbon_estimated = inference_engine.predict_for_region(
            roi=roi_for_calculation, year=year,
            start_month=start_month, end_month=end_month,
            cloud_threshold=cloud_threshold
        )

        carbon_estimated_display = carbon_estimated.clip(roi_original) if (clip_to_aoi and has_geojson) else carbon_estimated

        # Default vis_max is a static config value (e.g. 200) that rarely
        # matches this AOI's actual carbon density range, so the legend
        # ends up mostly unused (everything crammed into the low end of the
        # palette) or clipped (everything pegged at the top color). When the
        # caller hasn't explicitly pinned vis_min/vis_max, stretch the
        # estimated tile to this AOI's own robust 2nd/98th percentile
        # instead of the fixed default — same approach already used for the
        # reference-dataset tile below.
        carbon_stats = carbon_estimated.clip(roi_for_calculation).reduceRegion(
            reducer=ee.Reducer.mean()
                .combine(ee.Reducer.stdDev(), "", True)
                .combine(ee.Reducer.min(), "", True)
                .combine(ee.Reducer.max(), "", True)
                .combine(ee.Reducer.percentile([2, 98]), "", True),
            geometry=roi_for_calculation,
            scale=carbon_scale,
            maxPixels=config_service.get_analysis_defaults(db)["max_pixels"],
            bestEffort=True
        ).getInfo()

        _user_pinned_vis = "vis_min" in data or "vis_max" in data
        _default_vis = vis_params
        if not _user_pinned_vis:
            _est_min = carbon_stats.get("carbon_estimated_p2")
            _est_max = carbon_stats.get("carbon_estimated_p98")
            if _est_min is not None and _est_max is not None and _est_max > _est_min:
                _default_vis = {"min": _est_min, "max": _est_max, "palette": vis_palette}

        estimated_vis_params = _estimated_carbon_vis_params(
            _default_vis,
            ref_vis_params,
            dataset_info,
            match_reference=match_reference_vis,
        )

        mean_carbon = carbon_stats.get("carbon_estimated_mean", 0)
        total_carbon_tons = mean_carbon * calculation_area

        estimated_map_id = carbon_estimated_display.visualize(**_gee_visualize_params(estimated_vis_params)).getMapId()
        estimated_tile_url = estimated_map_id["tile_fetcher"].url_format

        stats_out = {
            "mean": round(mean_carbon, 2),
            "std_dev": round(carbon_stats.get("carbon_estimated_stdDev", 0), 2),
            "min": round(carbon_stats.get("carbon_estimated_min", 0), 2),
            "max": round(carbon_stats.get("carbon_estimated_max", 0), 2),
        }
    else:
        # ── Server-side sampled sklearn inference (RF, GB, etc.) ─────────
        sampled_stats = inference_engine.predict_for_region_sampled(
            roi=roi_for_calculation, year=year,
            start_month=start_month, end_month=end_month,
            cloud_threshold=cloud_threshold,
            scale=carbon_scale,
            n_samples=3000,
        )

        mean_carbon = sampled_stats["mean"]
        total_carbon_tons = mean_carbon * calculation_area
        estimated_tile_url = None
        estimated_vis_params = _estimated_carbon_vis_params(
            vis_params, ref_vis_params, dataset_info, match_reference=match_reference_vis
        )

        stats_out = {
            "mean": round(sampled_stats["mean"], 2),
            "std_dev": round(sampled_stats["std"], 2),
            "min": round(sampled_stats["min"], 2),
            "max": round(sampled_stats["max"], 2),
            "n_pixels": sampled_stats["n_pixels"],
        }

    target_unit = _compat_meta.get("target_unit", "Mg/ha")

    return {
        "carbon_estimated": {
            "tile_url": estimated_tile_url,
            "statistics": stats_out,
            "unit": target_unit,
            "vis_params": estimated_vis_params,
            "inference_mode": "gee_tile" if is_gee_deployable else "sampled_sklearn",
        },
        "carbon_reference": {
            "tile_url": reference_tile_url,
            "name": dataset_info["name"],
            "full_name": dataset_info["full_name"],
            "year": dataset_info["year"],
            "resolution": dataset_info["resolution"],
            "unit": dataset_info.get("unit", "Mg/ha"),
            "target_pool": dataset_info.get("target_pool"),
            "gee_id": dataset_info.get("gee_id"),
            "arcgis_service_url": dataset_info.get("arcgis_service_url"),
            "service_url": dataset_info.get("service_url"),
            "provider_type": dataset_info.get("provider_type", "gee"),
            "is_temporal": dataset_info.get("is_temporal", False),
            "requested_year": dataset_info.get("requested_year"),
            "description": dataset_info["description"],
            "vis_params": ref_vis_params,
            "statistics": arcgis_reference_stats or external_reference_stats,
        },
        "area_info": {
            "calculation_mode": calculation_mode,
            "original_aoi_area_ha": round(original_area, 2),
            "filtering_area_ha": round(filtering_area, 2),
            "calculation_area_ha": round(calculation_area, 2),
            "total_carbon_tons": round(total_carbon_tons, 2),
            "carbon_dioxide_equivalent_tons": round(total_carbon_tons * co2_factor, 2),
        },
        "model_info": {
            "model_name": inference_engine.model_name,
            "algorithm": model_info["algorithm"],
            "trained_at": model_info.get("trained_at"),
            "training_samples": model_info.get("n_samples"),
            "cv_metrics": model_info.get("cv_metrics", {}),
            "feature_importance": model_info.get("feature_importance", {}),
            "inference_date_range": f"{year}-{start_month:02d} to {year}-{end_month:02d}",
            "reference_dataset": reference_dataset,
            "scale": carbon_scale,
            "images_used": inference_engine.last_s2_image_count,
            "co2_conversion_factor": co2_factor,
            "gee_deployable": is_gee_deployable,
        },
    }


def analyze_carbon_local(db: Session, data: dict) -> dict:
    """Non-GEE carbon estimation: features come from Microsoft Planetary
    Computer STAC (Sentinel-2/DEM/WorldCover) via LocalCarbonInferenceEngine,
    never ee.Image. Does NOT require EE_INITIALIZED. Only works with models
    trained via training/train_carbon_non_gee.py (metadata provider in
    {"non_gee_stac","local_raster"}, gee_deployable=False) — model_name is
    required (no silent fallback to the GEE registry default, since that
    would mix a GEE-trained model's coefficients with STAC-sourced features).

    Response envelope deliberately mirrors analyze_carbon's non-gee-deployable
    ("sampled_sklearn") branch shape (carbon_estimated / carbon_reference /
    area_info / model_info) — only inference_mode differs ("sampled_stac" vs
    "sampled_sklearn"), and tile_url is always null (no tile map for this
    pipeline yet).
    """
    import time as _time

    _t0 = _time.perf_counter()
    settings = get_settings()

    if "aoi" not in data:
        raise AnalysisError("Missing required field: aoi", 400)
    model_name = data.get("model_name")
    if not model_name:
        raise AnalysisError("Missing required field: model_name (no default — must name a non-GEE model explicitly)", 400)

    aoi = data["aoi"]
    if "geojson" in aoi:
        from app.providers.external_carbon_provider import _geojson_to_bbox
        bbox = _geojson_to_bbox(aoi["geojson"])
    else:
        bbox = (aoi["west"], aoi["south"], aoi["east"], aoi["north"])

    start_date = data.get("start_date", "2022-01-01")
    end_date = data.get("end_date", "2022-12-31")
    scale = float(data.get("scale", 250))
    n_samples = int(data.get("n_samples", 2000))
    vis_min = int(data.get("vis_min", config_service.get_analysis_defaults(db)["carbon_vis_min"]))
    vis_max = int(data.get("vis_max", config_service.get_analysis_defaults(db)["carbon_vis_max"]))
    vis_palette = data.get("vis_palette", config_service.get_analysis_defaults(db)["carbon_vis_palette"])

    from pyproj import Geod

    model_path = get_active_model_path(db, "carbon", model_name)
    engine = LocalCarbonInferenceEngine(model_name=model_name, model_path=model_path)
    meta = model_compatibility.backfill_legacy_metadata(dict(engine.model.metadata), engine.model_name)
    if meta.get("gee_deployable"):
        raise AnalysisError(
            f"Model '{engine.model_name}' is GEE-trained (gee_deployable=True). "
            "Use /api/analyze/carbon for GEE-based models.",
            400,
        )

    try:
        stats = engine.predict_for_region_sampled(
            bbox, start_date, end_date, scale=scale, n_samples=n_samples,
        )
    except ValueError as e:
        raise AnalysisError(str(e), 422)

    west, south, east, north = bbox
    geod = Geod(ellps="WGS84")
    area_m2, _ = geod.polygon_area_perimeter(
        [west, east, east, west, west], [south, south, north, north, south]
    )
    calculation_area_ha = abs(area_m2) / 10000
    co2_factor = config_service.get_analysis_defaults(db)["carbon_co2_factor"]
    total_carbon_tons = stats["mean"] * calculation_area_ha

    target_dataset_key = meta.get("target_dataset_key")
    dataset_info = get_dataset_meta(target_dataset_key) if target_dataset_key else {}

    return {
        "carbon_estimated": {
            "tile_url": None,
            "statistics": {
                "mean": round(stats["mean"], 2),
                "std_dev": round(stats["std"], 2),
                "min": round(stats["min"], 2),
                "max": round(stats["max"], 2),
                "n_pixels": stats["n_pixels"],
            },
            "unit": meta.get("target_unit", "Mg/ha"),
            "vis_params": {"min": vis_min, "max": vis_max, "palette": vis_palette},
            "inference_mode": "sampled_stac",
        },
        "carbon_reference": {
            "tile_url": None,
            "name": dataset_info.get("name", target_dataset_key),
            "full_name": dataset_info.get("full_name", target_dataset_key),
            "year": dataset_info.get("year"),
            "resolution": dataset_info.get("resolution"),
        },
        "area_info": {
            "calculation_mode": "full_tiles",
            "original_aoi_area_ha": round(calculation_area_ha, 2),
            "filtering_area_ha": round(calculation_area_ha, 2),
            "calculation_area_ha": round(calculation_area_ha, 2),
            "total_carbon_tons": round(total_carbon_tons, 2),
            "carbon_dioxide_equivalent_tons": round(total_carbon_tons * co2_factor, 2),
        },
        "model_info": {
            "model_name": engine.model_name,
            "algorithm": meta.get("algorithm"),
            "trained_at": meta.get("trained_at"),
            "training_samples": meta.get("n_samples"),
            "cv_metrics": meta.get("cv_metrics", {}),
            "feature_importance": meta.get("feature_importance") or {},
            "inference_date_range": f"{start_date} to {end_date}",
            "reference_dataset": target_dataset_key,
            "scale": scale,
            "images_used": None,
            "co2_conversion_factor": co2_factor,
            "gee_deployable": False,
            "provider": meta.get("provider"),
            "caveat_note": meta.get("caveat_note"),
        },
        "processing_time": round(_time.perf_counter() - _t0, 1),
    }


def analyze_carbon_delta(db: Session, data: dict) -> dict:
    settings = get_settings()
    if "aoi" not in data:
        raise AnalysisError("Missing required field: aoi", 400)

    year_max_default = datetime.now().year
    start_year = int(data.get("start_year", year_max_default - 1))
    end_year = int(data.get("end_year", year_max_default))
    interval = max(1, int(data.get("interval", 1)))
    if start_year >= end_year:
        raise AnalysisError("start_year harus lebih kecil dari end_year", 400)

    year_min, year_max = 2015, year_max_default
    start_year = max(year_min, min(year_max, start_year))
    end_year = max(year_min, min(year_max, end_year))

    start_month = int(data.get("start_month", 1))
    end_month = int(data.get("end_month", 12))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    carbon_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["carbon_scale"]))
    model_name = data.get("model_name")

    if not (1 <= start_month <= 12 and 1 <= end_month <= 12 and start_month <= end_month):
        raise AnalysisError("Rentang bulan tidak valid", 400)

    roi = create_geometry_from_payload(data["aoi"])

    model_path = get_active_model_path(db, "carbon", model_name)
    inference_engine = CarbonInferenceEngine(model_name=model_name, model_path=model_path)
    model_info = inference_engine.get_model_info()

    years = list(range(start_year, end_year + 1, interval))
    if years[-1] != end_year:
        years.append(end_year)

    series = []
    for year in years:
        series.append(calculate_carbon_summary_for_year(
            db=db,
            inference_engine=inference_engine,
            roi=roi,
            year=year,
            start_month=start_month,
            end_month=end_month,
            cloud_threshold=cloud_threshold,
            carbon_scale=carbon_scale,
        ))

    deltas = []
    for previous, current in zip(series[:-1], series[1:]):
        total_delta = current["total_carbon_tons"] - previous["total_carbon_tons"]
        density_delta = current["mean_density"] - previous["mean_density"]
        co2_delta = current["carbon_dioxide_equivalent_tons"] - previous["carbon_dioxide_equivalent_tons"]
        deltas.append({
            "from_year": previous["year"],
            "to_year": current["year"],
            "delta_total_carbon_tons": round(total_delta, 2),
            "delta_mean_density": round(density_delta, 2),
            "delta_co2e_tons": round(co2_delta, 2),
            "delta_total_carbon_percent": round((total_delta / previous["total_carbon_tons"]) * 100, 2) if previous["total_carbon_tons"] else 0,
            "direction": "increase" if total_delta > 0 else ("decrease" if total_delta < 0 else "stable"),
        })

    return {
        "series": series,
        "deltas": deltas,
        "summary": {
            "start_year": series[0]["year"] if series else start_year,
            "end_year": series[-1]["year"] if series else end_year,
            "start_total_carbon_tons": series[0]["total_carbon_tons"] if series else 0,
            "end_total_carbon_tons": series[-1]["total_carbon_tons"] if series else 0,
            "net_delta_total_carbon_tons": round((series[-1]["total_carbon_tons"] - series[0]["total_carbon_tons"]) if len(series) > 1 else 0, 2),
            "net_delta_co2e_tons": round((series[-1]["carbon_dioxide_equivalent_tons"] - series[0]["carbon_dioxide_equivalent_tons"]) if len(series) > 1 else 0, 2),
        },
        "parameters": {
            "start_month": start_month,
            "end_month": end_month,
            "cloud_threshold": cloud_threshold,
            "scale": carbon_scale,
            "interval": interval,
        },
        "model_info": {
            "model_name": inference_engine.model_name,
            "algorithm": model_info.get("algorithm"),
            "scale": carbon_scale,
        },
    }
