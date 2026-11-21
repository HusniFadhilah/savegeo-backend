"""Crop Monitoring orchestrator (GEE-heavy).

Reuses existing engines rather than reimplementing them:
  - Sentinel-2 compositing/index math: `vegetation_service._composite_for_period`
    (satellite selection, cloud masking, band standardization, valid-pixel %)
    + `gee_common.calculate_index`. That engine is **month-granularity by
    design** (`build_date_range(year, start_month, end_month)`), so an
    arbitrary day-range monitoring period (30d/90d/custom) is approximated
    into a `(year, start_month, end_month)` window by `_period_to_year_months`
    below - a deliberate precision trade-off documented there, not a bug.
  - Vegetation time series (min/max/mean/trend/anomaly-flags per period):
    delegates wholesale to `vegetation_service.analyze_vegetation`'s
    `periods=[...]` mode.
  - Flood impact: `disaster_analysis_service.compute_flood_change` (public
    alias of `_compute_flood_change`), called directly with the Field's
    `ee.Geometry` + two dates - no persisted `DisasterAOI`/`AnalysisRun`.
  - Weather: `weather_service.get_weather_stats` (GEE CHIRPS+ERA5-Land or
    Open-Meteo, selectable).
  - Growth stage: pure Python, `crop_registry.resolve_growth_stage`.

Request contract for `run_crop_monitoring(db, data)` (raw dict, no Pydantic -
matches `vegetation.py`/`landcover.py`'s convention, see `app/schemas/
common.py`):

    {
      "field_id": int,                         # required
      "period": {                              # optional, default current_season
        "mode": "current_season"|"30d"|"90d"|"custom",
        "start_date": "YYYY-MM-DD",            # required if mode == "custom"
        "end_date": "YYYY-MM-DD",               # required if mode == "custom"
      },
      "sub_analyses": [...],                   # optional, default _DEFAULT_SUB_ANALYSES
      "cloud_threshold": int, "scale": int,     # optional, default from config_service
      "satellite": str, "cloud_mask_technique": str,   # optional
      "weather_source": "gee"|"openmeteo",      # optional, default "gee"
      "timeseries_index": str,                  # optional, default "NDVI" (sub-analysis B)
      "historical_years": int,                  # optional, default 3 (sub-analysis C)
      "flood": {"pre_date": "...", "post_date": "..."},  # required if "flood" requested
      "productivity_zone_periods": [{"year":.., "start_month":.., "end_month":..}, ...],  # optional (sub-analysis H)
      "data_sources": {"sentinel1": bool, ...}, # optional, only "sentinel1" consulted (H)
      "compare_years": [int, ...],              # optional (sub-analysis I)
    }

"flood" and "historical_comparison" are NOT in the default sub-analyses list
(they need extra params the caller must supply explicitly).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
from contextvars import ContextVar

import ee
from sqlalchemy.orm import Session

from app.registries import crop_registry
from app.registries.satellite_provider_registry import resolve_satellite
from app.registries.weather_provider_registry import resolve_weather_provider
from app.repositories import field_repo
from app.services import config_service, disaster_analysis_service, vegetation_service, weather_service, crop_result_cache, landcover_service
from app.services.gee_common import (
    AnalysisError,
    _event_area_ha,
    calculate_index,
    create_geometry_from_payload,
    get_tile_url,
    resolve_cloud_mask_technique,
    public_analysis_error,
)
from app.services.geo_utils import bbox_and_centroid

logger = logging.getLogger(__name__)

_DEFAULT_SUB_ANALYSES = (
    "landcover", "health", "timeseries", "anomaly", "growth_stage",
    "water_moisture", "weather", "productivity_zones", "risk_score",
)

_MOISTURE_STRESS_NDMI_THRESHOLD = 0.0
_ANOMALY_NDVI_DROP_THRESHOLD = -0.15

_COMPOSITES: ContextVar[dict | None] = ContextVar("crop_composites", default=None)


def _composite_for_period(db, aoi, year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique):
    """Reuse an EE composite within one analysis, isolated across fields/threads."""
    cache = _COMPOSITES.get()
    key = (year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique)
    if cache is not None and key in cache:
        return cache[key]
    result = vegetation_service._composite_for_period(db, aoi, year, start_month, end_month,
                                                     cloud_threshold, satellite, cloud_mask_technique)
    if cache is not None:
        cache[key] = result
    return result


def crop_summary_scale(area_ha, requested_scale):
    if not math.isfinite(area_ha) or area_ha <= 0 or requested_scale <= 0:
        raise AnalysisError("Luas lahan dan resolusi analisis harus positif.", 422)
    return max(requested_scale, math.ceil(math.sqrt(area_ha * 10000 / 250_000) / 10) * 10)


def _landcover(aoi, year, start_month, end_month, scale):
    image, metadata = landcover_service.get_landcover_image(
        "Dynamic_World", year, aoi, start_month, end_month, include_diagnostics=False,
    )
    result = landcover_service.summarize_landcover_image("Dynamic_World", image, aoi, max(10, scale), metadata)
    if not result:
        return {"available": False, "reason": "Tidak ada piksel tutupan lahan yang valid."}
    crops = result["classes"].get("Crops", {})
    return {"available": True, **result, "dataset": "Dynamic_World", "native_scale": 10,
            "cropland_area_ha": crops.get("area", 0), "cropland_percentage": crops.get("percentage", 0)}


# ─────────────────────────────────────────────
# Period resolution
# ─────────────────────────────────────────────

def _resolve_period(field, period: dict) -> tuple[str, str, str]:
    mode = period.get("mode", "current_season")
    today = dt.datetime.now(dt.UTC).date()
    if mode == "30d":
        return (today - dt.timedelta(days=30)).isoformat(), today.isoformat(), mode
    if mode == "90d":
        return (today - dt.timedelta(days=90)).isoformat(), today.isoformat(), mode
    if mode == "custom":
        start, end = period.get("start_date"), period.get("end_date")
        if not start or not end:
            raise AnalysisError("period.start_date dan period.end_date wajib diisi untuk mode custom", 400)
        return start, end, mode
    start = field.planting_date.isoformat() if field.planting_date else (today - dt.timedelta(days=90)).isoformat()
    return start, today.isoformat(), "current_season"


def _period_to_year_months(start_date: str, end_date: str) -> tuple[int, int, int]:
    """Approximates an arbitrary day-range into the `(year, start_month,
    end_month)` shape `vegetation_service._composite_for_period` expects -
    deliberate precision trade-off (see module docstring), not a bug. A
    range spanning into a following year is clamped to end_month=12 (covers
    through the start year's year-end, not into the next year)."""
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    year = start.year
    start_month = start.month
    end_month = end.month if end.year == start.year else 12
    end_month = max(end_month, start_month)
    return year, start_month, end_month


# ─────────────────────────────────────────────
# A. Crop Health
# ─────────────────────────────────────────────

def _crop_health(db, aoi, year, start_month, end_month, cloud_threshold, veg_scale, satellite, cloud_mask_technique) -> dict:
    composite, _, _, _, size, valid_pct = _composite_for_period(
        db, aoi, year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique
    )
    if composite is None:
        return {"available": False, "reason": "Tidak ada citra untuk periode ini"}

    ndvi_img = calculate_index(composite, "NDVI")
    stats = vegetation_service._index_stats(ndvi_img, "NDVI", aoi, veg_scale)
    classification = vegetation_service.summarize_vegetation_classes(ndvi_img, "NDVI", aoi, veg_scale)
    mean = stats["mean"]

    if mean is None:
        label = "Unknown"
    elif mean >= 0.6:
        label = "Good"
    elif mean >= 0.4:
        label = "Moderate"
    else:
        label = "Poor"

    classes = (classification or {}).get("classes", {})
    healthy_pct = round(sum(v["percentage"] for k, v in classes.items() if k in ("Vegetasi sedang", "Vegetasi rapat / sehat")), 1)
    moderate_pct = round(sum(v["percentage"] for k, v in classes.items() if k == "Vegetasi rendah"), 1)
    stressed_pct = round(sum(v["percentage"] for k, v in classes.items() if k in ("Tanah terbuka / vegetasi sangat jarang", "Non-vegetasi (air/bangunan/awan)")), 1)

    # Period-over-period change: current month-range composite vs the
    # immediately preceding calendar month. Named explicitly (not "10-day
    # change") since the underlying engine is month-granularity - see
    # `_period_to_year_months`'s docstring for why.
    change_pct = None
    prev_month, prev_year = (start_month - 1, year) if start_month > 1 else (12, year - 1)
    prev_composite, *_ = _composite_for_period(
        db, aoi, prev_year, prev_month, prev_month, cloud_threshold, satellite, cloud_mask_technique
    )
    if prev_composite is not None and mean is not None:
        prev_stats = vegetation_service._index_stats(calculate_index(prev_composite, "NDVI"), "NDVI", aoi, veg_scale)
        if prev_stats["mean"]:
            change_pct = round((mean - prev_stats["mean"]) / abs(prev_stats["mean"]) * 100, 1)

    return {
        "available": True,
        "tile_url": vegetation_service.vegetation_classification_tile(ndvi_img, "NDVI", "Crop health"),
        "native_scale": 10 if satellite == "sentinel2" else 30,
        "health_label": label,
        "ndvi_mean": round(mean, 4) if mean is not None else None,
        "ndvi_stats": stats,
        "classification": classification,
        "healthy_pct": healthy_pct,
        "moderate_pct": moderate_pct,
        "stressed_pct": stressed_pct,
        "change_vs_previous_month_pct": change_pct,
        "images_used": size,
        "valid_pixel_pct": valid_pct,
    }


# ─────────────────────────────────────────────
# B. Vegetation Time Series (delegates to vegetation_service wholesale)
# ─────────────────────────────────────────────

def _build_monthly_periods(year: int, start_month: int, end_month: int) -> list[dict]:
    months = range(start_month, end_month + 1) if end_month >= start_month else [start_month]
    return [{"year": year, "start_month": m, "end_month": m, "label": f"{year}-{m:02d}"} for m in months]


def _vegetation_timeseries(db, field, year, start_month, end_month, index_name, cloud_threshold, veg_scale, satellite, cloud_mask_technique) -> dict:
    periods = _build_monthly_periods(year, start_month, end_month)
    payload = {
        "aoi": {"geojson": field.geojson},
        "periods": periods,
        "indices": [index_name],
        "classify": False,
        "histogram": False,
        "scale": veg_scale,
        "cloud_threshold": cloud_threshold,
        "satellite": satellite,
        "cloud_mask_technique": cloud_mask_technique,
    }
    result = vegetation_service.analyze_vegetation(db, payload)
    series = result.get("time_series", {}).get(index_name, [])
    summary = result.get("time_series_summary", {}).get(index_name, {})
    means = sorted(p["mean"] for p in series if p["mean"] is not None)
    last_with_data = next((p for p in reversed(series) if p.get("date_range")), None)

    n = len(means)
    median = None
    if n:
        mid = n // 2
        median = means[mid] if n % 2 else round((means[mid - 1] + means[mid]) / 2, 4)

    return {
        "available": bool(means),
        "index": index_name,
        "periods": series,
        "min": min(means) if means else None,
        "max": max(means) if means else None,
        "mean": round(sum(means) / n, 4) if means else None,
        "median": median,
        "trend": summary.get("trend"),
        "slope": summary.get("slope"),
        "anomaly_periods": summary.get("anomaly_periods"),
        "last_image_date": last_with_data["date_range"]["end"] if last_with_data else None,
    }


# ─────────────────────────────────────────────
# C. Crop Anomaly
# ─────────────────────────────────────────────

def _anomaly_category(diff_pct: float | None) -> str:
    if diff_pct is None:
        return "Unknown"
    if diff_pct >= -5:
        return "Normal"
    if diff_pct >= -15:
        return "Watch"
    if diff_pct >= -30:
        return "Moderate"
    if diff_pct >= -50:
        return "High"
    return "Critical"


def _crop_anomaly(db, aoi, year, start_month, end_month, cloud_threshold, veg_scale, satellite, cloud_mask_technique, historical_years: int) -> dict:
    composite, *_ = _composite_for_period(db, aoi, year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique)
    if composite is None:
        return {"available": False, "reason": "Tidak ada citra untuk periode saat ini"}

    current_ndvi_img = calculate_index(composite, "NDVI")
    current_stats = vegetation_service._index_stats(current_ndvi_img, "NDVI", aoi, veg_scale)
    current_mean = current_stats["mean"]
    if current_mean is None:
        return {"available": False, "reason": "NDVI periode saat ini tidak tersedia"}

    hist_means, hist_images = [], []
    for y in range(1, historical_years + 1):
        h_composite, *_ = _composite_for_period(db, aoi, year - y, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique)
        if h_composite is None:
            continue
        h_ndvi = calculate_index(h_composite, "NDVI")
        h_stats = vegetation_service._index_stats(h_ndvi, "NDVI", aoi, veg_scale)
        if h_stats["mean"] is not None:
            hist_means.append(h_stats["mean"])
            hist_images.append(h_ndvi)

    if not hist_means:
        return {"available": False, "reason": "Data historis tidak cukup untuk baseline anomali"}

    historical_expected = sum(hist_means) / len(hist_means)
    diff_pct = round((current_mean - historical_expected) / abs(historical_expected) * 100, 1) if historical_expected else None

    historical_mean_img = ee.ImageCollection(hist_images).mean()
    delta_img = current_ndvi_img.subtract(historical_mean_img)
    anomaly_mask = delta_img.lte(_ANOMALY_NDVI_DROP_THRESHOLD).selfMask()
    affected_area_ha = _event_area_ha(anomaly_mask, aoi, veg_scale)
    tile = get_tile_url(delta_img, {"min": -0.3, "max": 0.3, "palette": ["#b71c1c", "#ffee58", "#2e7d32"]}, "Crop Anomaly")

    return {
        "available": True,
        "current_ndvi": round(current_mean, 4),
        "historical_expected_ndvi": round(historical_expected, 4),
        "difference_pct": diff_pct,
        "affected_area_ha": affected_area_ha,
        "category": _anomaly_category(diff_pct),
        "tile_url": tile["tile_url"] if tile else None,
        "historical_years_used": len(hist_means),
    }


# ─────────────────────────────────────────────
# D. Growth Stage (date-based only, V1 - no NDVI-phenology inference)
# ─────────────────────────────────────────────

def _growth_stage(field) -> dict:
    if field.planting_date is None:
        return {"available": False, "reason": "planting_date belum diisi pada Field ini"}
    days = (dt.datetime.now(dt.UTC).date() - field.planting_date).days
    stage = crop_registry.resolve_growth_stage(field.commodity, days)
    if stage is None:
        return {"available": False, "reason": "Growth stage tidak tersedia untuk komoditas ini"}
    return {
        "available": True,
        "stage": stage["label"],
        "stage_key": stage["key"],
        "crop_age_days": days,
        "confidence": "high",
        "estimated_harvest_date": field.estimated_harvest_date.isoformat() if field.estimated_harvest_date else None,
    }


# ─────────────────────────────────────────────
# E. Water & Moisture
# ─────────────────────────────────────────────

def _water_stress_label(weather_stats: dict | None) -> str:
    deficit = (weather_stats or {}).get("rainfall_deficit_pct")
    if deficit is None:
        return "Unknown"
    if deficit < 20:
        return "Low"
    if deficit < 50:
        return "Moderate"
    return "High"


def _water_moisture(db, aoi, year, start_month, end_month, cloud_threshold, veg_scale, satellite, cloud_mask_technique, weather_stats: dict) -> dict:
    composite, *_ = _composite_for_period(db, aoi, year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique)
    if composite is None:
        return {"available": False, "reason": "Tidak ada citra untuk periode ini"}

    ndmi_img = calculate_index(composite, "NDMI")
    ndmi_stats = vegetation_service._index_stats(ndmi_img, "NDMI", aoi, veg_scale)
    stress_mask = ndmi_img.lte(_MOISTURE_STRESS_NDMI_THRESHOLD).selfMask()
    stress_area_ha = _event_area_ha(stress_mask, aoi, veg_scale)

    return {
        "available": True,
        "ndmi_mean": round(ndmi_stats["mean"], 4) if ndmi_stats["mean"] is not None else None,
        "moisture_stress_area_ha": stress_area_ha,
        "rainfall": weather_stats,
        "water_stress_label": _water_stress_label(weather_stats),
    }


# ─────────────────────────────────────────────
# F. Weather Monitoring (V1: rule-based, already-observed data only - no forecast)
# ─────────────────────────────────────────────

def _weather_warnings(weather_stats: dict) -> list[dict]:
    warnings = []
    dry_days = weather_stats.get("dry_days")
    if dry_days is not None and dry_days >= 10:
        warnings.append({"level": "warning", "message": f"{dry_days} hari kering (curah hujan <1mm) dalam periode monitoring - risiko kekeringan."})
    tmax = weather_stats.get("tmax")
    if tmax is not None and tmax > 35:
        warnings.append({"level": "warning", "message": f"Suhu maksimum rata-rata {tmax}°C - potensi heat stress."})
    return warnings


def _weather_monitoring(weather_stats: dict) -> dict:
    return {"available": True, **weather_stats, "warnings": _weather_warnings(weather_stats)}


# ─────────────────────────────────────────────
# G. Flood / Excess-Water Impact
# ─────────────────────────────────────────────

def _flood_severity(pct: float | None) -> str:
    if pct is None:
        return "Unknown"
    if pct < 5:
        return "Low"
    if pct < 20:
        return "Moderate"
    if pct < 50:
        return "High"
    return "Critical"


def _flood_impact(aoi, field, flood_params: dict | None) -> dict:
    if not flood_params or not flood_params.get("pre_date") or not flood_params.get("post_date"):
        return {"available": False, "reason": "flood.pre_date dan flood.post_date diperlukan"}
    pre_date = dt.date.fromisoformat(flood_params["pre_date"])
    post_date = dt.date.fromisoformat(flood_params["post_date"])
    result = disaster_analysis_service.compute_flood_change(aoi, pre_date, post_date)
    flooded_ha = result["statistics"]["flooded_area_ha"]
    field_area = field.area_ha or 0
    pct = round(flooded_ha / field_area * 100, 1) if field_area else None
    return {"available": True, **result, "field_area_ha": field_area, "flooded_pct_of_field": pct, "severity": _flood_severity(pct)}


# ─────────────────────────────────────────────
# H. Field Productivity Zones
# ─────────────────────────────────────────────

def _s1_vv_median(aoi, start_date: str, end_date: str):
    col = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi).filterDate(start_date, end_date)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .select("VV")
    )
    if col.size().getInfo() == 0:
        return None
    return col.median().clip(aoi)


def _productivity_zones(db, aoi, period_list: list[tuple[int, int, int]], cloud_threshold, veg_scale, satellite, cloud_mask_technique, include_s1: bool) -> dict:
    ndvi_images, ndmi_images = [], []
    date_ranges = []
    for (y, sm, em) in period_list:
        composite, _, s_date, e_date, _size, _ = _composite_for_period(db, aoi, y, sm, em, cloud_threshold, satellite, cloud_mask_technique)
        if composite is None:
            continue
        ndvi_images.append(calculate_index(composite, "NDVI"))
        ndmi_images.append(calculate_index(composite, "NDMI"))
        date_ranges.append((s_date, e_date))

    if not ndvi_images:
        return {"available": False, "reason": "Tidak ada citra untuk zona produktivitas"}

    ndvi_norm = ee.ImageCollection(ndvi_images).median().add(1).divide(2)
    ndmi_norm = ee.ImageCollection(ndmi_images).median().add(1).divide(2)

    elevation = ee.Image("USGS/SRTMGL1_003").clip(aoi)
    elev_stats = elevation.reduceRegion(reducer=ee.Reducer.minMax(), geometry=aoi, scale=90, bestEffort=True, maxPixels=int(1e9)).getInfo()
    elev_min, elev_max = elev_stats.get("elevation_min"), elev_stats.get("elevation_max")
    elev_norm = elevation.subtract(elev_min).divide(elev_max - elev_min) if (elev_min is not None and elev_max is not None and elev_max > elev_min) else None

    s1_norm = None
    if include_s1 and date_ranges:
        s1_median = _s1_vv_median(aoi, date_ranges[0][0], date_ranges[-1][1])
        if s1_median is not None:
            s1_norm = s1_median.add(25).divide(25).clamp(0, 1)  # VV in dB, roughly [-25, 0] -> [0, 1]

    components: list[tuple[str, ee.Image, float]] = [("ndvi", ndvi_norm, 0.40), ("ndmi", ndmi_norm, 0.25)]
    if s1_norm is not None:
        components.append(("sentinel1_vv", s1_norm, 0.20))
    if elev_norm is not None:
        components.append(("elevation", elev_norm, 0.15))
    total_w = sum(w for _, _, w in components)

    score, weights_out = None, {}
    for name, img, w in components:
        norm_w = w / total_w
        weights_out[name] = round(norm_w, 3)
        term = img.multiply(norm_w)
        score = term if score is None else score.add(term)
    score = score.rename("productivity_score")

    high_mask, med_mask, low_mask = score.gte(0.66).selfMask(), score.gte(0.33).And(score.lt(0.66)).selfMask(), score.lt(0.33).selfMask()
    high_ha, med_ha, low_ha = _event_area_ha(high_mask, aoi, veg_scale), _event_area_ha(med_mask, aoi, veg_scale), _event_area_ha(low_mask, aoi, veg_scale)
    total = high_ha + med_ha + low_ha
    tile = get_tile_url(score, {"min": 0, "max": 1, "palette": ["#c62828", "#fdd835", "#2e7d32"]}, "Productivity Zones")

    def _zone(ha):
        return {"area_ha": ha, "percentage": round(ha / total * 100, 1) if total else 0.0}

    return {
        "available": True,
        "zones": {"High": _zone(high_ha), "Medium": _zone(med_ha), "Low": _zone(low_ha)},
        "tile_url": tile["tile_url"] if tile else None,
        "seasons_used": len(ndvi_images),
        "weights": weights_out,
    }


# ─────────────────────────────────────────────
# I. Historical Comparison
# ─────────────────────────────────────────────

def _build_historical_narrative(rows: list[dict]) -> str | None:
    valid = [r for r in rows if r["available"] and r["ndvi_mean"] is not None]
    if len(valid) < 2:
        return None
    current, previous = valid[0], valid[1]
    if not previous["ndvi_mean"]:
        return None
    delta_pct = round((current["ndvi_mean"] - previous["ndvi_mean"]) / abs(previous["ndvi_mean"]) * 100, 1)
    direction = "di atas" if delta_pct >= 0 else "di bawah"
    return (
        f"NDVI musim {current['year']} berada sekitar {abs(delta_pct)}% {direction} musim "
        f"{previous['year']} pada periode pemantauan yang sama."
    )


def _historical_comparison(db, aoi, year, start_month, end_month, cloud_threshold, veg_scale, satellite, cloud_mask_technique, compare_years: list[int] | None) -> dict:
    years = compare_years or [year, year - 1, year - 2]
    rows = []
    for y in years:
        composite, *_ = _composite_for_period(db, aoi, y, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique)
        if composite is None:
            rows.append({"year": y, "ndvi_mean": None, "available": False})
            continue
        stats = vegetation_service._index_stats(calculate_index(composite, "NDVI"), "NDVI", aoi, veg_scale)
        rows.append({"year": y, "ndvi_mean": round(stats["mean"], 4) if stats["mean"] is not None else None, "available": stats["mean"] is not None})

    return {"available": any(r["available"] for r in rows), "years": rows, "narrative": _build_historical_narrative(rows)}


# ─────────────────────────────────────────────
# J. Crop Risk Score (weights admin-configurable via system_config, not hardcoded)
# ─────────────────────────────────────────────

def _clip01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _risk_level(pct: float | None) -> str:
    if pct is None:
        return "Unknown"
    if pct < 25:
        return "Low"
    if pct < 50:
        return "Moderate"
    if pct < 75:
        return "High"
    return "Critical"


def _risk_score(db: Session, sub_results: dict) -> dict:
    weights = {
        "vegetation": float(config_service.get_setting(db, "crop_risk.weight_vegetation", 0.30)),
        "moisture": float(config_service.get_setting(db, "crop_risk.weight_moisture", 0.20)),
        "weather": float(config_service.get_setting(db, "crop_risk.weight_weather", 0.20)),
        "flood": float(config_service.get_setting(db, "crop_risk.weight_flood", 0.15)),
        "growth_anomaly": float(config_service.get_setting(db, "crop_risk.weight_growth_anomaly", 0.15)),
    }

    scores: dict[str, float] = {}
    anomaly = sub_results.get("anomaly")
    if anomaly and anomaly.get("available"):
        diff = anomaly.get("difference_pct") or 0
        scores["vegetation"] = _clip01(max(0.0, -diff) / 50)

    water = sub_results.get("water_moisture")
    if water and water.get("available"):
        deficit = (water.get("rainfall") or {}).get("rainfall_deficit_pct")
        scores["moisture"] = _clip01((deficit or 0) / 60) if deficit else 0.0

    weather = sub_results.get("weather")
    if weather and weather.get("available"):
        scores["weather"] = _clip01(len(weather.get("warnings", [])) / 2)

    flood = sub_results.get("flood")
    if flood and flood.get("available"):
        scores["flood"] = _clip01((flood.get("flooded_pct_of_field") or 0) / 50)

    # V1 has no growth-stage anomaly detection (date-based lookup only, no
    # NDVI-phenology comparison to flag "behind/ahead of expected stage") -
    # always 0 until that V2 capability exists, not silently dropped from
    # the weighted sum (still shown in `breakdown` at weight, score=0).
    if sub_results.get("growth_stage", {}).get("available"):
        scores["growth_anomaly"] = 0.0

    total_weight = sum(weights[k] for k in scores) or 1.0
    weighted = sum(scores[k] * weights[k] for k in scores)
    score_0_100 = round(weighted / total_weight * 100) if total_weight else 0

    breakdown = [
        {"factor": k, "score": round(scores[k] * 100, 1), "weight": weights[k], "level": _risk_level(scores[k] * 100)}
        for k in scores
    ]
    return {"available": bool(scores), "score": score_0_100, "level": _risk_level(score_0_100), "weights": weights, "breakdown": breakdown}


# ─────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────

def run_crop_monitoring(db: Session, data: dict, owner_key: str) -> dict:
    token = _COMPOSITES.set({})
    try:
        return _run_crop_monitoring(db, data, owner_key)
    finally:
        _COMPOSITES.reset(token)


def _run_crop_monitoring(db: Session, data: dict, owner_key: str) -> dict:
    field_id = data.get("field_id")
    if not field_id:
        raise AnalysisError("field_id is required", 400)
    field = field_repo.get_field(db, int(field_id), owner_key)
    if field is None:
        raise AnalysisError("Field tidak ditemukan", 404)
    aoi = create_geometry_from_payload({"geojson": field.geojson})
    defaults = config_service.get_analysis_defaults(db)
    cloud_threshold = int(data.get("cloud_threshold", defaults["cloud_threshold"]))
    requested_scale = int(data.get("scale", defaults["veg_scale"]))
    veg_scale = crop_summary_scale(field.area_ha, requested_scale)
    satellite = resolve_satellite(data.get("satellite"))
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))
    weather_source = resolve_weather_provider(data.get("weather_source"))
    start_date, end_date, period_mode = _resolve_period(field, data.get("period") or {})
    year, start_month, end_month = _period_to_year_months(start_date, end_date)
    requested = data.get("sub_analyses")
    if requested is None:
        requested = list(_DEFAULT_SUB_ANALYSES)
    if not isinstance(requested, list) or not requested or any(not isinstance(k, str) or k not in {*_DEFAULT_SUB_ANALYSES, "flood", "historical_comparison"} for k in requested):
        raise AnalysisError("Pilih bagian analisis tanaman yang valid.", 422)
    parameters = {k: v for k, v in data.items() if k not in {"sub_analyses", "field_id"}}
    parameters.update(start_date=start_date, end_date=end_date, scale=veg_scale, satellite=satellite,
                      cloud_threshold=cloud_threshold, cloud_mask_technique=cloud_mask_technique,
                      weather_source=weather_source, timeseries_index=data.get("timeseries_index", "NDVI"),
                      historical_years=int(data.get("historical_years", 3)), data_sources=data.get("data_sources") or {"sentinel1": True})
    results, skipped, timings, cache_hits = {}, [], {}, []
    weather_value = None

    def weather_inputs():
        nonlocal weather_value
        if weather_value is not None:
            return weather_value
        key = crop_result_cache.section_key(field, parameters, "weather_inputs")
        weather_value = crop_result_cache.load(db, key)
        if weather_value is None:
            _, centroid = bbox_and_centroid(field.geojson)
            if centroid is None:
                raise AnalysisError("Tidak bisa menghitung centroid field", 422)
            weather_value = weather_service.get_weather_stats(
                aoi, (centroid["lat"], centroid["lng"]), start_date, end_date, source=weather_source)
            crop_result_cache.save(db, key, field.id, "weather_inputs", weather_value)
        return weather_value

    args = (db, aoi, year, start_month, end_month, cloud_threshold, veg_scale, satellite, cloud_mask_technique)
    seasons = data.get("productivity_zone_periods")
    periods = [(p["year"], p["start_month"], p["end_month"]) for p in seasons] if seasons else [(year, start_month, end_month)]
    operations = {
        "landcover": lambda: _landcover(aoi, year, start_month, end_month, veg_scale),
        "health": lambda: _crop_health(*args),
        "timeseries": lambda: _vegetation_timeseries(db, field, year, start_month, end_month,
            data.get("timeseries_index", "NDVI"), cloud_threshold, veg_scale, satellite, cloud_mask_technique),
        "anomaly": lambda: _crop_anomaly(*args, int(data.get("historical_years", 3))),
        "growth_stage": lambda: _growth_stage(field),
        "water_moisture": lambda: _water_moisture(*args, weather_inputs()),
        "weather": lambda: _weather_monitoring(weather_inputs()),
        "flood": lambda: _flood_impact(aoi, field, data.get("flood")),
        "productivity_zones": lambda: _productivity_zones(db, aoi, periods, cloud_threshold, veg_scale,
            satellite, cloud_mask_technique, bool((data.get("data_sources") or {}).get("sentinel1", True))),
        "historical_comparison": lambda: _historical_comparison(*args, data.get("compare_years")),
        "risk_score": lambda: _risk_score(db, results),
    }
    for section in [k for k in requested if k != "risk_score"] + (["risk_score"] if "risk_score" in requested else []):
        started = time.monotonic()
        # Risk depends on this response's selected sections; it is cheap and must
        # not reuse a score generated from a different selection.
        key = crop_result_cache.section_key(field, parameters, section)
        cached = crop_result_cache.load(db, key) if section != "risk_score" else None
        if cached is not None:
            results[section] = cached
            cache_hits.append(section)
        else:
            try:
                results[section] = operations[section]()
                if section != "risk_score":
                    crop_result_cache.save(db, key, field.id, section, results[section])
            except Exception as exc:  # noqa: BLE001 - one unavailable section must not discard other results
                logger.warning("Crop section %s failed (%s)", section, type(exc).__name__, exc_info=True)
                reason = public_analysis_error(exc) if isinstance(exc, AnalysisError) else "Bagian ini belum berhasil diproses; coba ulang atau gunakan area/periode lebih kecil."
                results[section] = {"available": False, "reason": reason}
                skipped.append({"sub_analysis": section, "reason": reason})
        timings[section] = round(time.monotonic() - started, 3)
    return {
        "field": field.to_dict(include_geojson=True),
        "period": {"start": start_date, "end": end_date, "mode": period_mode,
                   "resolved_year_month_range": {"year": year, "start_month": start_month, "end_month": end_month}},
        "cloud_threshold": cloud_threshold, "scale": veg_scale, "requested_scale": requested_scale,
        "satellite": satellite, "weather_source": weather_source,
        "sub_analyses": results, "skipped": skipped,
        "performance": {"cache_hits": cache_hits, "section_seconds": timings, "cache_ttl_seconds": crop_result_cache.TTL_SECONDS},
    }
