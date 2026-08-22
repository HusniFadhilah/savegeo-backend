"""Weather/rainfall data for Crop Monitoring (sub-analyses E/F). Two
implementations behind one normalized shape, selected by `source`:

  - "gee": CHIRPS Daily (`UCSB-CHG/CHIRPS/DAILY`) for rainfall, ERA5-Land
    Daily Aggregates (`ECMWF/ERA5_LAND/DAILY_AGGR`) for tmax/tmin/humidity
    (humidity derived from temperature + dewpoint via the Magnus formula -
    ERA5-Land has no direct relative-humidity band). Area-averaged over the
    field polygon.
  - "openmeteo": Open-Meteo's free historical archive API
    (archive-api.open-meteo.com), no API key. Point estimate at the field's
    centroid, not area-averaged. Humidity is not requested (the archive
    API's daily aggregates don't include it without a separate hourly
    fetch) - reported as `None` with a `data_quality.note`, not a fabricated
    value.

V1 has no forward-looking forecast on either path (see the Crop Monitoring
implementation plan's "Weather forecast" decision) - both only look at
already-elapsed date ranges.

Rainfall's historical-normal baseline (used for `rainfall_deficit_pct` by
sub-analysis E) is the mean of the same start/end day-of-year window across
the `historical_years` years immediately preceding the period - not a
climatological normal, a best-effort recent-years average.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

import ee
import httpx

from app.services.gee_common import AnalysisError

logger = logging.getLogger(__name__)

_OPENMETEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_DEFAULT_HISTORICAL_YEARS = 3
_CHIRPS_SCALE = 5000  # CHIRPS native resolution is ~5.5km
_ERA5_SCALE = 11132  # ERA5-Land native resolution is ~11km


def shift_years(date_str: str, years: int) -> str:
    """`date_str` ("YYYY-MM-DD") shifted back by `years`, Feb-29 falls back
    to Feb-28 in the target (non-leap) year."""
    d = dt.date.fromisoformat(date_str)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:  # Feb 29 in a non-leap target year
        return d.replace(year=d.year - years, day=28).isoformat()


# ─────────────────────────────────────────────
# GEE path (CHIRPS + ERA5-Land)
# ─────────────────────────────────────────────

def _chirps_total_mm(aoi, start_date: str, end_date: str) -> float | None:
    col = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterBounds(aoi).filterDate(start_date, end_date)
    if col.size().getInfo() == 0:
        return None
    total = col.select("precipitation").sum()
    val = total.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi, scale=_CHIRPS_SCALE, bestEffort=True, maxPixels=int(1e9)
    ).get("precipitation")
    result = val.getInfo() if val is not None else None
    return float(result) if result is not None else None


def _chirps_daily_series(aoi, start_date: str, end_date: str) -> list[float]:
    """Per-day area-mean rainfall (mm), one server round trip via
    `aggregate_array` rather than one `.getInfo()` per image."""
    col = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterBounds(aoi).filterDate(start_date, end_date)

    def _reduce(img):
        val = img.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=aoi, scale=_CHIRPS_SCALE, bestEffort=True, maxPixels=int(1e9)
        ).get("precipitation")
        return img.set("precip_mm", val)

    values = col.map(_reduce).aggregate_array("precip_mm").getInfo()
    return [float(v) for v in values if v is not None]


def _era5_temp_humidity(aoi, start_date: str, end_date: str) -> dict[str, float | None]:
    col = (
        ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .select(["temperature_2m_max", "temperature_2m_min", "temperature_2m", "dewpoint_temperature_2m"])
    )
    if col.size().getInfo() == 0:
        return {"tmax": None, "tmin": None, "humidity": None}

    stats = col.mean().reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi, scale=_ERA5_SCALE, bestEffort=True, maxPixels=int(1e9)
    ).getInfo()

    def _k_to_c(k):
        return round(k - 273.15, 1) if k is not None else None

    tmax = _k_to_c(stats.get("temperature_2m_max"))
    tmin = _k_to_c(stats.get("temperature_2m_min"))
    t_c, td_c = _k_to_c(stats.get("temperature_2m")), _k_to_c(stats.get("dewpoint_temperature_2m"))

    humidity = None
    if t_c is not None and td_c is not None:
        # Magnus formula: RH = 100 * e(Td) / e(T)
        es = 6.112 * math.exp((17.67 * t_c) / (t_c + 243.5))
        e = 6.112 * math.exp((17.67 * td_c) / (td_c + 243.5))
        humidity = round(max(0.0, min(100.0, (e / es) * 100)), 1)

    return {"tmax": tmax, "tmin": tmin, "humidity": humidity}


def _weather_gee(aoi, start_date: str, end_date: str, historical_years: int) -> dict[str, Any]:
    try:
        precip_series = _chirps_daily_series(aoi, start_date, end_date)
    except ee.EEException as exc:
        raise AnalysisError(f"Gagal mengambil data curah hujan CHIRPS: {exc}", 502) from exc

    rainfall_mm_period = round(sum(precip_series), 1)
    dry_days = sum(1 for v in precip_series if v < 1.0)
    images_used = len(precip_series)

    historical_totals = []
    for y in range(1, historical_years + 1):
        h_start, h_end = shift_years(start_date, y), shift_years(end_date, y)
        try:
            total = _chirps_total_mm(aoi, h_start, h_end)
        except ee.EEException:
            total = None
        if total is not None:
            historical_totals.append(total)
    rainfall_mm_historical_normal = round(sum(historical_totals) / len(historical_totals), 1) if historical_totals else None

    try:
        temp_humidity = _era5_temp_humidity(aoi, start_date, end_date)
    except ee.EEException as exc:
        logger.warning("ERA5-Land fetch failed: %s", exc)
        temp_humidity = {"tmax": None, "tmin": None, "humidity": None}

    rainfall_deficit_pct = None
    if rainfall_mm_historical_normal:
        rainfall_deficit_pct = round((rainfall_mm_historical_normal - rainfall_mm_period) / rainfall_mm_historical_normal * 100, 1)

    return {
        "source": "gee",
        "period": {"start": start_date, "end": end_date},
        "rainfall_mm_period": rainfall_mm_period,
        "rainfall_mm_historical_normal": rainfall_mm_historical_normal,
        "rainfall_deficit_pct": rainfall_deficit_pct,
        "tmax": temp_humidity["tmax"],
        "tmin": temp_humidity["tmin"],
        "humidity": temp_humidity["humidity"],
        "dry_days": dry_days,
        "data_quality": {
            "images_used": images_used,
            "note": None if historical_totals else "Data historis (CHIRPS) tidak cukup untuk baseline rainfall_mm_historical_normal",
        },
    }


# ─────────────────────────────────────────────
# Open-Meteo path (point estimate at field centroid)
# ─────────────────────────────────────────────

def _openmeteo_daily(lat: float, lon: float, start_date: str, end_date: str) -> dict[str, Any]:
    try:
        resp = httpx.get(
            _OPENMETEO_ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": start_date,
                "end_date": end_date,
                "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
            timeout=20,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AnalysisError(f"Gagal mengambil data Open-Meteo: {exc}", 502) from exc
    return resp.json().get("daily", {})


def _mean(values: list[float]) -> float | None:
    valid = [v for v in values if v is not None]
    return round(sum(valid) / len(valid), 1) if valid else None


def _weather_openmeteo(lat: float, lon: float, start_date: str, end_date: str, historical_years: int) -> dict[str, Any]:
    daily = _openmeteo_daily(lat, lon, start_date, end_date)
    precip = daily.get("precipitation_sum") or []
    rainfall_mm_period = round(sum(v for v in precip if v is not None), 1)
    dry_days = sum(1 for v in precip if v is not None and v < 1.0)

    historical_totals = []
    for y in range(1, historical_years + 1):
        h_start, h_end = shift_years(start_date, y), shift_years(end_date, y)
        try:
            h_daily = _openmeteo_daily(lat, lon, h_start, h_end)
        except AnalysisError:
            continue
        h_precip = [v for v in (h_daily.get("precipitation_sum") or []) if v is not None]
        if h_precip:
            historical_totals.append(sum(h_precip))
    rainfall_mm_historical_normal = round(sum(historical_totals) / len(historical_totals), 1) if historical_totals else None

    rainfall_deficit_pct = None
    if rainfall_mm_historical_normal:
        rainfall_deficit_pct = round((rainfall_mm_historical_normal - rainfall_mm_period) / rainfall_mm_historical_normal * 100, 1)

    return {
        "source": "openmeteo",
        "period": {"start": start_date, "end": end_date},
        "rainfall_mm_period": rainfall_mm_period,
        "rainfall_mm_historical_normal": rainfall_mm_historical_normal,
        "rainfall_deficit_pct": rainfall_deficit_pct,
        "tmax": _mean(daily.get("temperature_2m_max") or []),
        "tmin": _mean(daily.get("temperature_2m_min") or []),
        "humidity": None,
        "dry_days": dry_days,
        "data_quality": {
            "images_used": None,
            "note": "Kelembapan (humidity) tidak tersedia dari Open-Meteo archive API daily aggregates.",
        },
    }


# ─────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────

def get_weather_stats(
    aoi,
    centroid: tuple[float, float],
    start_date: str,
    end_date: str,
    source: str = "gee",
    historical_years: int = _DEFAULT_HISTORICAL_YEARS,
) -> dict[str, Any]:
    """`centroid` is `(lat, lon)`. `aoi` (an `ee.Geometry`) is only required
    for `source == "gee"`; Open-Meteo only needs the centroid."""
    if source == "openmeteo":
        return _weather_openmeteo(centroid[0], centroid[1], start_date, end_date, historical_years)
    return _weather_gee(aoi, start_date, end_date, historical_years)
