"""Carbon dataset discovery helpers — DB + registry lookups only, no GEE calls —
plus (appended below) the GEE-heavy carbon analysis route-body logic.

Discovery helpers ported from legacy `app.py::get_carbon_dataset_list` /
`_active_carbon_model_compatibility`. Analysis helpers ported from legacy
`app.py::calculate_carbon_summary_for_year` (lines ~1623-1679) and the
`/api/analyze/carbon*` route bodies (lines ~2765-3370).
"""
from __future__ import annotations

import itertools
import json
import logging
import time
from datetime import UTC, datetime

import ee
import requests
from sqlalchemy.orm import Session

from app.db.models.uploaded_model import UploadedModel
from app.inference.carbon_inference import CarbonInferenceEngine
from app.inference.carbon_inference_local import LocalCarbonInferenceEngine
from app.providers.external_carbon_provider import ExternalRasterProvider
from app.registries import model_compatibility
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
from app.repositories import dataset_repo
from app.repositories.uploaded_model_repo import get_active_model_path
from app.services import config_service
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
    resolve_cloud_mask_technique,
)

logger = logging.getLogger(__name__)
_DATASET_HEALTH_CACHE: dict[str, tuple[float, dict]] = {}


def get_direct_carbon_reference_layer(
    dataset_key: str,
    dataset_year: int = 2020,
    vis_min: float | None = None,
    vis_max: float | None = None,
    vis_palette: list[str] | None = None,
) -> dict:
    """Build a global reference tile layer without AOI statistics/model inference.

    AOI remains required for summaries and totals, but a reference raster can
    be viewed globally using its native COG, ArcGIS, or Earth Engine tiles.
    """
    from app.services.arcgis_helpers import _gee_visualize_params

    meta = get_dataset_meta(dataset_key, dataset_year)
    available_years = meta.get("available_years")
    if available_years and dataset_year not in available_years:
        raise AnalysisError(
            f"Tahun {dataset_year} tidak tersedia untuk {dataset_key}. Pilih salah satu: {', '.join(map(str, available_years))}.",
            422,
        )
    selectable_year = bool(meta.get("is_temporal") or meta.get("mask_lossyear"))
    if selectable_year and not available_years:
        year_range = meta.get("year_range")
        if year_range and not year_range[0] <= dataset_year <= year_range[1]:
            raise AnalysisError(f"Tahun {dataset_year} di luar cakupan {dataset_key}.", 422)
    effective_year = dataset_year if available_years or selectable_year else meta.get("year")
    source_year = effective_year if isinstance(effective_year, int) else dataset_year
    ingestion = meta.get("ingestion_method")
    if ingestion == "soilgrids_rest":
        raise AnalysisError("SoilGrids hanya menyediakan statistik sampling; pilih AOI untuk memuat nilainya.", 422)

    tile_url = None
    if ingestion == "cog_rasterio":
        tile_url = f"/api/carbon/reference-tiles/{dataset_key}/{{z}}/{{x}}/{{y}}.png?year={source_year}"
    elif dataset_key in CARBON_ARCGIS_REGISTRY:
        tile_url = f"/api/arcgis/tiles/{dataset_key}/{{z}}/{{y}}/{{x}}?year={source_year}"
    else:
        if ingestion in {"cloud_geotiff_ee", "chloris_downloads_index"}:
            image = load_external_carbon_reference_ee(dataset_key, source_year, roi=None)
        elif dataset_key in CARBON_DATASET_REGISTRY:
            image = load_carbon_reference_ee(dataset_key, source_year, roi=None)
        else:
            raise AnalysisError(f"Dataset '{dataset_key}' tidak mendukung pemuatan langsung.", 422)
        vis = {
            "min": vis_min if vis_min is not None else meta.get("vis_min", 0),
            "max": vis_max if vis_max is not None else meta.get("vis_max", 300),
            "palette": vis_palette or meta.get("vis_palette") or ["440154", "fde725"],
        }
        map_id = image.getMapId(_gee_visualize_params(vis))
        tile_url = map_id["tile_fetcher"].url_format

    return {
        "dataset": dataset_key,
        "dataset_name": meta.get("name") or meta.get("full_name") or dataset_key,
        "full_name": meta.get("full_name"),
        "tile_url": tile_url,
        "year": effective_year,
        "requested_year": dataset_year,
        "effective_year": effective_year,
        "available_years": available_years,
        "resolution": meta.get("resolution"),
        "unit": meta.get("unit"),
        "target_pool": meta.get("target_pool"),
        "provider_type": meta.get("provider_type"),
        "vis_params": {
            "min": vis_min if vis_min is not None else meta.get("vis_min", 0),
            "max": vis_max if vis_max is not None else meta.get("vis_max", 300),
            "palette": vis_palette or meta.get("vis_palette") or [],
        },
        "direct": True,
        "statistics_available": False,
    }


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
    db: Session, provider: str | None = None, require_model: bool = True
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
            "ingestion_method": meta.get("ingestion_method"),
            "reference_only_capable": meta.get("ingestion_method") in {"cog_rasterio", "soilgrids_rest", "cloud_geotiff_ee", "chloris_downloads_index"} or key in CARBON_ARCGIS_REGISTRY or key in CARBON_DATASET_REGISTRY,
            "unit": meta.get("unit"),
            "target_pool": meta.get("target_pool"),
            "resolution": meta.get("resolution"),
            "year": meta.get("year"),
            "year_range": meta.get("year_range"),
            "available_years": meta.get("available_years"),
            "selection_year": meta.get("year") if isinstance(meta.get("year"), int) else (
                CARBON_DATASET_REGISTRY.get(key)
                or CARBON_ARCGIS_REGISTRY.get(key)
                or CARBON_EXTERNAL_REGISTRY.get(key)
                or {}
            ).get("year"),
            "year_selectable": bool(meta.get("available_years") or meta.get("is_temporal") or meta.get("mask_lossyear")),
            "deprecated": bool(meta.get("deprecated", False)),
            "replacement_key": meta.get("replacement_key"),
            "description": meta.get("description"),
            "attribution": meta.get("attribution"),
            "limitations": meta.get("limitations", []),
            "compatible_model_count": len(model_names),
            "compatible_models": model_names,
            "training_capable": meta.get("training_capable", key in CARBON_DATASET_REGISTRY),
            **_dataset_runtime_status(key, meta),
        })

    return result


def _dataset_runtime_status(key: str, meta: dict) -> dict:
    """Return lightweight runtime availability metadata for dataset pickers."""
    if meta.get("ingestion_method") == "mask_only":
        return {
            "is_configured": False,
            "requires_configuration": False,
            "availability_error": "Dataset ini hanya mask extent, bukan raster AGB/carbon yang dapat dianalisis.",
        }
    if key == "CHLORIS_AGB_STOCK" or meta.get("ingestion_method") == "chloris_downloads_index":
        from app.services.chloris_service import is_chloris_configured

        configured = is_chloris_configured()
        return {
            "is_configured": configured,
            "requires_configuration": not configured,
            "availability_error": None
            if configured
            else (
                "Isi CHLORIS_AGB_STOCK_URL atau CHLORIS_DATA_PATH, atau pastikan "
                "dependency pystac-client dan planetary-computer tersedia untuk "
                "fallback Planetary Computer. Alternatif: CHLORIS_ORGANIZATION_ID "
                "+ CHLORIS_REFRESH_TOKEN/CHLORIS_ID_TOKEN."
            ),
        }
    return {"is_configured": True, "requires_configuration": False, "availability_error": None}


def get_cached_external_dataset_health(key: str) -> dict:
    from app.core.config import get_settings

    cached = _DATASET_HEALTH_CACHE.get(key)
    if cached:
        ttl = max(60, int(getattr(get_settings(), "carbon_dataset_health_ttl_seconds", 900)))
        if time.time() - cached[0] > ttl:
            return {**cached[1], "status": "unknown", "stale": True}
        return cached[1]
    return {"key": key, "status": "unknown", "check_scope": "reachability_only", "error": None, "checked_at": None}


def check_external_dataset_health(key: str, refresh: bool = False) -> dict:
    """Check a public reference endpoint with a short, cached request."""
    from app.core.config import get_settings

    meta = CARBON_EXTERNAL_REGISTRY.get(key)
    if not meta:
        return {"key": key, "status": "unknown", "check_scope": "reachability_only", "error": "Dataset bukan external raster"}
    now = time.time()
    ttl = max(60, int(getattr(get_settings(), "carbon_dataset_health_ttl_seconds", 900)))
    cached = _DATASET_HEALTH_CACHE.get(key)
    if cached and not refresh and now - cached[0] <= ttl:
        return cached[1]

    url = meta.get("service_url")
    if meta.get("global_url_template"):
        years = meta.get("available_years") or [meta.get("year", 2025)]
        url = meta["global_url_template"].format(year=max(years))
    elif meta.get("tile_url_template"):
        template = meta["tile_url_template"]
        if "{tile_id}" in template:
            url = template.format(tile_id="N00E100", year=max(meta.get("available_years") or [meta.get("year", 2020)]))
        else:
            url = template.format(lat_tile="00N", lon_tile="100E")
    if not url:
        result = {"key": key, "status": "unknown", "check_scope": "reachability_only", "error": "Tidak ada URL health check"}
        _DATASET_HEALTH_CACHE[key] = (now, result)
        return result

    started = time.perf_counter()
    try:
        response = requests.head(url, timeout=5, allow_redirects=True)
        if response.status_code in {405, 501}:
            response.close()
            response = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=5, stream=True)
        try:
            ok = 200 <= response.status_code < 400
            result = {
                "key": key,
                "check_scope": "reachability_only",
                "status": "healthy" if ok else "unavailable",
                "http_status": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "checked_at": datetime.now(UTC).isoformat(),
                "error": None if ok else f"HTTP {response.status_code}",
            }
        finally:
            response.close()
    except requests.RequestException:
        result = {
            "key": key,
            "check_scope": "reachability_only",
            "status": "unavailable",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "checked_at": datetime.now(UTC).isoformat(),
            "error": "Health check request failed",
        }
    _DATASET_HEALTH_CACHE[key] = (now, result)
    return result


def _aoi_bbox_and_geometry(aoi: dict) -> tuple[tuple[float, float, float, float], dict | None]:
    """Return a WGS84 bbox and optional GeoJSON geometry for local rasters."""
    if aoi.get("geojson"):
        payload = aoi["geojson"]
        if payload.get("type") == "Feature":
            geometry = payload.get("geometry")
        elif payload.get("type") == "FeatureCollection":
            geometry = {
                "type": "GeometryCollection",
                "geometries": [feature["geometry"] for feature in payload.get("features", [])],
            }
        else:
            geometry = payload
        if not geometry:
            raise AnalysisError("AOI GeoJSON tidak memiliki geometry.", 400)

        coordinates: list[float] = []

        def collect(value):
            if isinstance(value, dict):
                if "coordinates" in value:
                    collect(value["coordinates"])
                if "geometries" in value:
                    collect(value["geometries"])
                return
            if isinstance(value, (list, tuple)):
                if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                    coordinates.extend([float(value[0]), float(value[1])])
                    return
                for child in value:
                    collect(child)

        collect(geometry.get("coordinates"))
        if len(coordinates) < 4:
            raise AnalysisError("AOI GeoJSON coordinates tidak valid.", 400)
        lons = coordinates[0::2]
        lats = coordinates[1::2]
        return (min(lons), min(lats), max(lons), max(lats)), geometry

    return (
        float(aoi["west"]), float(aoi["south"]),
        float(aoi["east"]), float(aoi["north"]),
    ), None


def _local_aoi_areas_ha(data: dict, bbox: tuple[float, float, float, float], geometry: dict | None) -> tuple[float, float, float]:
    """Compute AOI areas locally so COG-only analysis does not call EE getInfo()."""
    from pyproj import Geod
    from shapely.geometry import box, shape

    geod = Geod(ellps="WGS84")
    original_geometry = shape(geometry) if geometry is not None else box(*bbox)
    original_area = abs(geod.geometry_area_perimeter(original_geometry)[0]) / 10000.0
    bbox_area = abs(geod.geometry_area_perimeter(box(*bbox))[0]) / 10000.0
    calculation_area = original_area if data.get("clip_to_aoi", True) and geometry is not None else bbox_area
    return original_area, original_area, calculation_area


def _analyze_local_reference_only(
    data: dict,
    dataset_info: dict,
    dataset_meta: dict,
    dataset_year: int,
    roi_original,
    roi_for_calculation,
    roi_for_filtering,
    calculation_mode: str,
    vis_min: int,
    vis_max: int,
    vis_palette: list[str],
    db: Session,
) -> dict:
    """Calculate reference-only statistics from a tiled/global COG without GEE."""
    import numpy as np
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom
    from app.providers.local_raster_provider import GridSpec, load_carbon_reference_to_grid

    bbox, geometry = _aoi_bbox_and_geometry(data["aoi"])
    resolution = float(dataset_meta.get("resolution") or 100)
    grid = GridSpec(bbox, resolution_m=resolution)
    labels, load_info = load_carbon_reference_to_grid(dataset_info["key"], grid, year=dataset_year)

    valid = np.isfinite(labels)
    if geometry is not None:
        geometry_projected = transform_geom("EPSG:4326", grid.crs, geometry)
        inside = geometry_mask([geometry_projected], out_shape=grid.shape, transform=grid.transform, invert=True)
        valid &= inside
    values = labels[valid]
    if values.size == 0:
        raise AnalysisError(
            f"Dataset '{dataset_info['key']}' tidak memiliki nilai valid pada AOI.",
            422,
            extra={"dataset": dataset_info["key"]},
        )

    mean_carbon = float(np.mean(values))
    std_carbon = float(np.std(values))
    min_carbon = float(np.min(values))
    max_carbon = float(np.max(values))
    original_area, filtering_area, calculation_area = _local_aoi_areas_ha(data, bbox, geometry)
    total_carbon_tons = mean_carbon * calculation_area
    co2_factor = config_service.get_analysis_defaults(db)["carbon_co2_factor"]
    stats_out = {
        "mean": round(mean_carbon, 2),
        "std_dev": round(std_carbon, 2),
        "min": round(min_carbon, 2),
        "max": round(max_carbon, 2),
    }
    ref_vis_params = {
        "min": dataset_info.get("vis_min", vis_min),
        "max": dataset_info.get("vis_max", vis_max),
        "palette": dataset_info.get("vis_palette", vis_palette),
    }
    label_year = load_info.get("label_year", dataset_info.get("year"))
    reference_tile_url = (
        f"/api/carbon/reference-tiles/{dataset_info['key']}/{{z}}/{{x}}/{{y}}.png"
        f"?year={label_year}&min={ref_vis_params['min']}&max={ref_vis_params['max']}"
    )
    return {
        "carbon_estimated": {
            "tile_url": reference_tile_url,
            "statistics": stats_out,
            "unit": dataset_info.get("unit", "Mg C/ha"),
            "vis_params": ref_vis_params,
            "inference_mode": "reference_only",
        },
        "carbon_reference": {
            "tile_url": reference_tile_url,
            "name": dataset_info["name"],
            "full_name": dataset_info["full_name"],
            "year": label_year,
            "resolution": dataset_info.get("resolution"),
            "unit": dataset_info.get("unit", "Mg C/ha"),
            "target_pool": dataset_info.get("target_pool"),
            "gee_id": None,
            "service_url": dataset_info.get("service_url"),
            "provider_type": dataset_info.get("provider_type", "external_raster"),
            "is_temporal": dataset_info.get("is_temporal", False),
            "requested_year": dataset_info.get("requested_year"),
            "description": dataset_info["description"],
            "vis_params": ref_vis_params,
            "statistics": stats_out,
            "load_info": load_info,
        },
        "area_info": {
            "calculation_mode": calculation_mode,
            "original_aoi_area_ha": round(original_area, 2),
            "filtering_area_ha": round(filtering_area, 2),
            "calculation_area_ha": round(calculation_area, 2),
            "total_carbon_tons": round(total_carbon_tons, 2),
            "carbon_dioxide_equivalent_tons": round(total_carbon_tons * co2_factor, 2),
            "description": "Mode reference-only: statistik dihitung langsung dari COG referensi lokal, tanpa model estimasi SAVEGEO.",
        },
        "model_info": {
            "model_name": "reference_only",
            "algorithm": "direct_cog_raster",
            "calculation_mode": calculation_mode,
            "display_mode": "reference_only",
            "scale": resolution,
            "reference_dataset": dataset_info["key"],
            "reference_dataset_year": load_info.get("label_year", dataset_info.get("year")),
            "target_pool": dataset_info.get("target_pool"),
            # Reference-only mode has no Sentinel-2 inference year. Report the
            # selected raster vintage so the result cannot be mistaken for a
            # model prediction from the current imagery year.
            "analysis_year": label_year,
        },
    }


def _analyze_external_reference_only(
    data: dict,
    dataset_info: dict,
    dataset_meta: dict,
    dataset_year: int,
    db: Session,
) -> dict:
    """Analyze REST-backed references without constructing an EE geometry.

    SoilGrids returns SOC concentration and bulk density. We expose the SOC
    statistics and derive a 0-30 cm stock estimate in Mg C/ha from both layers.
    """
    from app.providers.external_carbon_provider import _geojson_to_bbox

    aoi = data["aoi"]
    aoi_geojson = aoi.get("geojson")
    if aoi_geojson:
        bbox = _geojson_to_bbox(aoi_geojson)
    else:
        bbox = (float(aoi["west"]), float(aoi["south"]), float(aoi["east"]), float(aoi["north"]))
        aoi_geojson = {
            "type": "Polygon",
            "coordinates": [[
                [bbox[0], bbox[1]], [bbox[2], bbox[1]],
                [bbox[2], bbox[3]], [bbox[0], bbox[3]], [bbox[0], bbox[1]],
            ]],
        }

    stats = ExternalRasterProvider().get_stats_for_aoi(dataset_meta, aoi_geojson)
    from pyproj import Geod
    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    geod = Geod(ellps="WGS84")
    geometry_payload = aoi_geojson
    if aoi_geojson.get("type") == "Feature":
        geometry_payload = aoi_geojson.get("geometry") or {}
    elif aoi_geojson.get("type") == "FeatureCollection":
        geometry_payload = None
        geometries = [feature.get("geometry") for feature in aoi_geojson.get("features", []) if feature.get("geometry")]
        if geometries:
            geometry = unary_union([shape(item) for item in geometries])
        else:
            geometry = box(*bbox)
    if geometry_payload is not None:
        geometry = shape(geometry_payload)
    area_ha = abs(geod.geometry_area_perimeter(geometry)[0]) / 10000.0
    if not data.get("clip_to_aoi", True):
        area_ha = abs(geod.geometry_area_perimeter(box(*bbox))[0]) / 10000.0
    stats_out = {key: stats.get(key) for key in ("mean", "std_dev", "min", "max")}
    stock_mean = stats.get("stock_mean")
    total_carbon_tons = stock_mean * area_ha if stock_mean is not None else None
    co2_factor = config_service.get_analysis_defaults(db)["carbon_co2_factor"]
    vis_params = {
        "min": dataset_info.get("vis_min", 0),
        "max": dataset_info.get("vis_max", 80),
        "palette": dataset_info.get("vis_palette", []),
    }
    return {
        "carbon_estimated": {
            "tile_url": None,
            "statistics": stats_out,
            "unit": dataset_info.get("unit", stats.get("unit", "")),
            "vis_params": vis_params,
            "inference_mode": "reference_only",
        },
        "carbon_reference": {
            "tile_url": None,
            "name": dataset_info["name"],
            "full_name": dataset_info["full_name"],
            "year": dataset_info.get("year"),
            "resolution": dataset_info.get("resolution"),
            "unit": dataset_info.get("unit", stats.get("unit", "")),
            "target_pool": dataset_info.get("target_pool"),
            "gee_id": None,
            "service_url": dataset_info.get("service_url"),
            "provider_type": dataset_info.get("provider_type", "external_raster"),
            "is_temporal": dataset_info.get("is_temporal", False),
            "requested_year": dataset_year,
            "description": dataset_info["description"],
            "vis_params": vis_params,
            "statistics": stats_out,
            "load_info": {
                "method": "soilgrids_rest",
                "n_samples": stats.get("n_samples"),
                "derived_stock_mean_mg_c_ha": stock_mean,
                "derived_stock_n_samples": stats.get("stock_n_samples"),
                "rock_fragment_correction_applied": stats.get("rock_fragment_correction_applied", False),
                "rock_fragment_mean_pct": stats.get("rock_fragment_mean_pct"),
                "rock_fragment_n_samples": stats.get("rock_fragment_n_samples", 0),
            },
        },
        "area_info": {
            "calculation_mode": "clipped_aoi" if data.get("clip_to_aoi", True) else "full_tiles",
            "original_aoi_area_ha": round(area_ha, 2),
            "filtering_area_ha": round(area_ha, 2),
            "calculation_area_ha": round(area_ha, 2),
            "total_carbon_tons": round(total_carbon_tons, 2) if total_carbon_tons is not None else None,
            "carbon_dioxide_equivalent_tons": round(total_carbon_tons * co2_factor, 2) if total_carbon_tons is not None else None,
            "description": "SoilGrids menampilkan SOC (g/kg) dan stok 0-30 cm turunan dari SOC, bulk density bdod, serta koreksi coarse/rock fragments cfvo per lapisan.",
        },
        "model_info": {
            "model_name": "reference_only",
            "algorithm": "soilgrids_rest",
            "calculation_mode": "reference_only",
            "display_mode": "reference_only",
            "scale": dataset_info.get("resolution"),
            "reference_dataset": dataset_info["key"],
            "reference_dataset_year": dataset_year,
            "target_pool": dataset_info.get("target_pool"),
            # SoilGrids is a static 2017 product; the Sentinel-2 year is not
            # used by this direct reference calculation.
            "analysis_year": dataset_year,
        },
        "data_quality": {
            "valid_pixel_pct": None,
            "gap_filled": None,
            "images_used": stats.get("n_samples"),
            "coefficient_of_variation_pct": round((stats["std_dev"] / stats["mean"]) * 100, 1) if stats.get("mean") else None,
            "model_r2": None,
            "model_rmse": None,
        },
    }


# ─────────────────────────────────────────────
# GEE-heavy analysis helpers / route-body logic
# ─────────────────────────────────────────────

def calculate_carbon_summary_for_year(
    db, inference_engine, roi, year, start_month, end_month, cloud_threshold, carbon_scale,
    include_tile: bool = False, vis_params: dict | None = None,
):
    analysis_defaults = config_service.get_analysis_defaults(db)
    meta = inference_engine.model.metadata
    gee_algo_type = meta.get("gee_algorithm_type") or (
        "native_classifier" if meta.get("gee_classifier_path")
        else ("linear_expression" if meta.get("gee_deployable") else "server_side_only")
    )

    area_ha = geometry_area_ha(roi)
    co2_factor = analysis_defaults["carbon_co2_factor"]
    tile_url = None

    if gee_algo_type in ("linear_expression", "native_classifier"):
        carbon_image = inference_engine.predict_for_region(
            roi=roi, year=year,
            start_month=start_month, end_month=end_month,
            cloud_threshold=cloud_threshold,
        ).clip(roi)
        stats = carbon_image.reduceRegion(
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

        # Timelapse playback needs a tile per year, not just scalars - only
        # generated on request (include_tile) since getMapId has real latency
        # and a chart-only caller (analyze_carbon_delta's default) doesn't need it.
        if include_tile:
            _vis = vis_params or {
                "min": analysis_defaults["carbon_vis_min"],
                "max": analysis_defaults["carbon_vis_max"],
                "palette": analysis_defaults["carbon_vis_palette"],
            }
            try:
                map_id = carbon_image.visualize(**_gee_visualize_params(_vis)).getMapId()
                tile_url = map_id["tile_fetcher"].url_format
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Timelapse tile generation failed for year {year}: {e}")
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
        # No raster to tile for sampled/server-side-only models (point predictions only).

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
        "tile_url": tile_url,
        # Per-year data quality (P0) - lets a year-over-year swing be checked
        # against composite quality instead of assumed to be real biomass
        # change. `images_used` reset per-call in both inference paths (see
        # CarbonInferenceEngine) so this is this year's own count, not a
        # cross-year cumulative total.
        "images_used": inference_engine.last_s2_image_count,
        "valid_pixel_pct": inference_engine.last_valid_pixel_pct,
        "gap_filled": inference_engine.last_gap_filled,
    }


def analyze_carbon(db: Session, data: dict) -> dict:
    if "aoi" not in data:
        raise AnalysisError("Missing required field: aoi", 400)

    year = int(data.get("year"))
    start_month = int(data.get("start_month"))
    end_month = int(data.get("end_month"))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    clip_to_aoi = data.get("clip_to_aoi", True)
    model_name = data.get("model_name")
    dataset_year = int(data.get("dataset_year", 2010))
    reference_dataset = data.get("reference_dataset", "WCMC")
    reference_only = bool(data.get("reference_only")) or (
        not model_name
        and (
                reference_dataset == "CHLORIS_AGB_STOCK"
                or (
                    reference_dataset in CARBON_EXTERNAL_REGISTRY
                    and get_external_carbon_meta(reference_dataset).get("ingestion_method") == "cog_rasterio"
                )
                or (
                    reference_dataset in CARBON_EXTERNAL_REGISTRY
                    and get_external_carbon_meta(reference_dataset).get("ingestion_method") == "soilgrids_rest"
                )
                or reference_dataset in CARBON_DATASET_REGISTRY
        )
    )
    vis_min = int(data.get("vis_min", config_service.get_analysis_defaults(db)["carbon_vis_min"]))
    vis_max = int(data.get("vis_max", config_service.get_analysis_defaults(db)["carbon_vis_max"]))
    vis_palette = data.get("vis_palette", config_service.get_analysis_defaults(db)["carbon_vis_palette"])
    carbon_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["carbon_scale"]))
    match_reference_vis = bool(data.get("match_reference_vis", True))
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))

    # Audit: "S2 SR vs TOA provenance for 2015" - carbon inference always
    # composites Sentinel-2 via COPERNICUS/S2_SR_HARMONIZED (see
    # CarbonInferenceEngine._safe_s2_composite), whose real global L2A data
    # starts 2017-03-28, not the satellite's 2015 launch date - verified
    # live: requesting 2015/2016 returns zero images for every AOI tried.
    # 2015/2016 used to pass this check and fail confusingly deep inside GEE
    # processing instead; reject upfront with a clear reason.
    year_min, year_max = 2017, datetime.now(UTC).year
    if not reference_only and not (year_min <= year <= year_max):
        raise AnalysisError(
            f"Year harus antara {year_min}-{year_max} (Sentinel-2 Surface Reflectance belum tersedia sebelum {year_min})",
            400,
        )

    dataset_info = get_dataset_meta(reference_dataset, dataset_year)

    # SoilGrids is a REST reference and must remain usable when Earth Engine
    # credentials are unavailable. Handle it before constructing any ee.Geometry.
    if (
        reference_only
        and reference_dataset in CARBON_EXTERNAL_REGISTRY
        and CARBON_EXTERNAL_REGISTRY[reference_dataset].get("ingestion_method") == "soilgrids_rest"
    ):
        return _analyze_external_reference_only(data, dataset_info, CARBON_EXTERNAL_REGISTRY[reference_dataset], dataset_year, db)

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
        and _external_ref_meta.get("ingestion_method") in {"cloud_geotiff_ee", "chloris_downloads_index"}
    )
    if (
        reference_only
        and _is_external_ref
        and _external_ref_meta is not None
        and _external_ref_meta.get("ingestion_method") == "cog_rasterio"
    ):
        return _analyze_local_reference_only(
            data=data,
            dataset_info=dataset_info,
            dataset_meta=_external_ref_meta,
            dataset_year=dataset_year,
            roi_original=roi_original,
            roi_for_calculation=roi_for_calculation,
            roi_for_filtering=roi_for_filtering,
            calculation_mode=calculation_mode,
            vis_min=vis_min,
            vis_max=vis_max,
            vis_palette=vis_palette,
            db=db,
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

    if reference_only:
        if carbon_reference is None:
            raise AnalysisError(
                f"Dataset '{reference_dataset}' belum mendukung mode reference-only.",
                422,
                extra={"dataset": reference_dataset},
            )

        ref_vis_params = {
            "min": dataset_info.get("vis_min", vis_min),
            "max": dataset_info.get("vis_max", vis_max),
            "palette": dataset_info.get("vis_palette", vis_palette),
        }
        reference_display = carbon_reference.clip(roi_for_calculation)
        reference_tile_url = None
        try:
            reference_tile_url = (
                reference_display.visualize(**_gee_visualize_params(ref_vis_params))
                .getMapId()["tile_fetcher"].url_format
            )
        except Exception as ref_tile_err:  # noqa: BLE001
            logger.warning(f"Reference-only tile failed for '{reference_dataset}': {ref_tile_err}")

        try:
            reference_stats_raw = reference_display.reduceRegion(
                reducer=ee.Reducer.mean()
                    .combine(ee.Reducer.stdDev(), "", True)
                    .combine(ee.Reducer.min(), "", True)
                    .combine(ee.Reducer.max(), "", True),
                geometry=roi_for_calculation,
                scale=carbon_scale,
                maxPixels=config_service.get_analysis_defaults(db)["max_pixels"],
                bestEffort=True,
                tileScale=4,
            ).getInfo()
        except Exception as ref_stats_err:  # noqa: BLE001
            logger.warning(f"Reference-only stats failed for '{reference_dataset}': {ref_stats_err}")
            reference_stats_raw = {}

        original_area = geometry_area_ha(roi_original)
        calculation_area = geometry_area_ha(roi_for_calculation)
        filtering_area = geometry_area_ha(roi_for_filtering)
        mean_carbon = float(reference_stats_raw.get("agb_mean") or 0)
        std_carbon = float(reference_stats_raw.get("agb_stdDev") or 0)
        min_carbon = float(reference_stats_raw.get("agb_min") or 0)
        max_carbon = float(reference_stats_raw.get("agb_max") or 0)
        total_carbon_tons = mean_carbon * calculation_area
        co2_factor = config_service.get_analysis_defaults(db)["carbon_co2_factor"]
        stats_out = {
            "mean": round(mean_carbon, 2),
            "std_dev": round(std_carbon, 2),
            "min": round(min_carbon, 2),
            "max": round(max_carbon, 2),
        }

        return {
            "carbon_estimated": {
                "tile_url": None,
                "statistics": stats_out,
                "unit": dataset_info.get("unit", "Mg C/ha"),
                "vis_params": ref_vis_params,
                "inference_mode": "reference_only",
            },
            "carbon_reference": {
                "tile_url": reference_tile_url,
                "name": dataset_info["name"],
                "full_name": dataset_info["full_name"],
                "year": dataset_info["year"],
                "resolution": dataset_info["resolution"],
                "unit": dataset_info.get("unit", "Mg C/ha"),
                "target_pool": dataset_info.get("target_pool"),
                "gee_id": dataset_info.get("gee_id"),
                "service_url": dataset_info.get("service_url"),
                "provider_type": dataset_info.get("provider_type", "gee"),
                "is_temporal": dataset_info.get("is_temporal", False),
                "requested_year": dataset_info.get("requested_year"),
                "description": dataset_info["description"],
                "vis_params": ref_vis_params,
                "statistics": stats_out,
            },
            "area_info": {
                "calculation_mode": calculation_mode,
                "original_aoi_area_ha": round(original_area, 2),
                "filtering_area_ha": round(filtering_area, 2),
                "calculation_area_ha": round(calculation_area, 2),
                "total_carbon_tons": round(total_carbon_tons, 2),
                "carbon_dioxide_equivalent_tons": round(total_carbon_tons * co2_factor, 2),
                "description": (
                    f"Mode reference-only: total dihitung langsung dari raster "
                    f"referensi {dataset_info.get('name') or reference_dataset} "
                    "pada AOI, tanpa model estimasi SAVEGEO."
                ),
            },
            "model_info": {
                "model_name": "reference_only",
                "algorithm": "direct_raster",
                "calculation_mode": calculation_mode,
                "display_mode": "reference_only",
                "scale": carbon_scale,
                "reference_dataset": reference_dataset,
                "reference_dataset_year": dataset_info.get("year"),
                "target_pool": dataset_info.get("target_pool"),
                # No Sentinel-2 inference is performed in reference-only mode;
                # expose the selected reference vintage as the effective year.
                "analysis_year": dataset_info.get("year", dataset_year),
            },
            "data_quality": {
                "valid_pixel_pct": None,
                "gap_filled": None,
                "images_used": None,
                "coefficient_of_variation_pct": round((std_carbon / mean_carbon) * 100, 1) if mean_carbon else None,
                "model_r2": None,
                "model_rmse": None,
            },
        }

    # Cari model dari DB
    model_path = get_active_model_path(db, "carbon", model_name)
    inference_engine = CarbonInferenceEngine(model_name=model_name, model_path=model_path, cloud_mask_technique=cloud_mask_technique)
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

    _stat_mean = stats_out.get("mean") or 0
    _stat_std = stats_out.get("std_dev") or 0
    _cv_pct = round((_stat_std / _stat_mean) * 100, 1) if _stat_mean else None

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
            "model_version": _db_model.version if _db_model else None,
            "algorithm": model_info["algorithm"],
            "trained_at": model_info.get("trained_at"),
            "training_samples": model_info.get("n_samples"),
            "cv_metrics": model_info.get("cv_metrics", {}),
            "feature_importance": model_info.get("feature_importance", {}),
            "inference_date_range": f"{year}-{start_month:02d} to {year}-{end_month:02d}",
            # Audit: "carbon reference vs model-estimate labeling for 2025/2026" -
            # `year` here is the satellite-imagery year the model ran inference on;
            # `reference_dataset`'s own vintage (see carbon_reference.year, e.g.
            # WCMC circa-2010) is almost always older. Exposing `analysis_year`
            # explicitly lets the frontend show "this is a model prediction for
            # {analysis_year} imagery, trained on {reference_dataset} ground truth
            # from {reference.year}" instead of implying a direct measurement.
            "analysis_year": year,
            "reference_dataset": reference_dataset,
            "scale": carbon_scale,
            "images_used": inference_engine.last_s2_image_count,
            "co2_conversion_factor": co2_factor,
            "gee_deployable": is_gee_deployable,
        },
        # P0 "confidence & data quality": cv_metrics above is the model's own
        # training-time accuracy (R²/RMSE, static per model); this block is
        # per-run signal about *this specific* AOI/date-range composite.
        # `coefficient_of_variation_pct` describes spatial variability inside
        # the AOI, not a statistical confidence interval - reporting a formal
        # CI would falsely assume spatially independent pixels, which isn't
        # true for imagery (neighboring pixels are strongly correlated).
        "data_quality": {
            "valid_pixel_pct": inference_engine.last_valid_pixel_pct,
            "gap_filled": inference_engine.last_gap_filled,
            "images_used": inference_engine.last_s2_image_count,
            "coefficient_of_variation_pct": _cv_pct,
            "model_r2": (model_info.get("cv_metrics") or {}).get("r2_mean"),
            "model_rmse": (model_info.get("cv_metrics") or {}).get("rmse_mean"),
            "cloud_mask_technique": cloud_mask_technique,
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
    scale = float(data.get("scale", 10))
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
    if "aoi" not in data:
        raise AnalysisError("Missing required field: aoi", 400)

    year_max_default = datetime.now(UTC).year
    start_year = int(data.get("start_year", year_max_default - 1))
    end_year = int(data.get("end_year", year_max_default))
    interval = max(1, int(data.get("interval", 1)))
    if start_year >= end_year:
        raise AnalysisError("start_year harus lebih kecil dari end_year", 400)

    # Same Sentinel-2 SR floor as analyze_carbon (2017, not 2015 - see note there).
    year_min, year_max = 2017, year_max_default
    start_year = max(year_min, min(year_max, start_year))
    end_year = max(year_min, min(year_max, end_year))

    start_month = int(data.get("start_month", 1))
    end_month = int(data.get("end_month", 12))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    carbon_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["carbon_scale"]))
    model_name = data.get("model_name")
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))

    if not (1 <= start_month <= 12 and 1 <= end_month <= 12 and start_month <= end_month):
        raise AnalysisError("Rentang bulan tidak valid", 400)

    roi = create_geometry_from_payload(data["aoi"])

    model_path = get_active_model_path(db, "carbon", model_name)
    inference_engine = CarbonInferenceEngine(model_name=model_name, model_path=model_path, cloud_mask_technique=cloud_mask_technique)
    model_info = inference_engine.get_model_info()

    # Timelapse tiles are opt-in - each one costs a real getMapId() round trip
    # per year, unnecessary for a chart-only caller.
    include_tiles = bool(data.get("include_tiles", False))
    analysis_defaults = config_service.get_analysis_defaults(db)
    tile_vis_params = {
        "min": int(data.get("vis_min", analysis_defaults["carbon_vis_min"])),
        "max": int(data.get("vis_max", analysis_defaults["carbon_vis_max"])),
        "palette": data.get("vis_palette", analysis_defaults["carbon_vis_palette"]),
    }

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
            include_tile=include_tiles,
            vis_params=tile_vis_params,
        ))

    deltas = []
    for previous, current in itertools.pairwise(series):
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
        # Keep the timelapse legend tied to the exact parameters used by
        # Image.visualize() above. The client must not reconstruct these from
        # a separately cached configuration value.
        "visualization": {
            "min": tile_vis_params["min"],
            "max": tile_vis_params["max"],
            "palette": tile_vis_params["palette"],
            "legend_bins": analysis_defaults["carbon_legend_bins"],
        },
        "model_info": {
            "model_name": inference_engine.model_name,
            "algorithm": model_info.get("algorithm"),
            "scale": carbon_scale,
            "cloud_mask_technique": cloud_mask_technique,
        },
    }
