"""Safe server-side NASA FIRMS adapter.

The MAP_KEY is deliberately used only in this module. Responses contain
normalized observations and cache metadata, never upstream URLs or secrets.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import requests

from app.core.config import get_settings

OFFICIAL_HOST = "firms.modaps.eosdis.nasa.gov"
FIRMS_SOURCE_CATALOG = {
    "VIIRS_NOAA20_NRT": {"label": "VIIRS NOAA-20", "sensor": "VIIRS", "resolution_m": 375},
    "VIIRS_NOAA21_NRT": {"label": "VIIRS NOAA-21", "sensor": "VIIRS", "resolution_m": 375},
    "VIIRS_SNPP_NRT": {"label": "VIIRS Suomi-NPP", "sensor": "VIIRS", "resolution_m": 375},
    "MODIS_NRT": {"label": "MODIS Terra/Aqua", "sensor": "MODIS", "resolution_m": 1000},
    "LANDSAT_NRT": {"label": "Landsat NRT", "sensor": "Landsat", "resolution_m": 30},
}
WMS_LAYERS = {
    "viirs_24h": "fires_viirs_24",
    "viirs_48h": "fires_viirs_48",
    "viirs_72h": "fires_viirs_72",
    "viirs_7d": "fires_viirs_7",
    "modis_24h": "fires_modis_24",
    "modis_7d": "fires_modis_7",
}
MAX_BBOX_SPAN = 60.0
MAX_BBOX_AREA = 2500.0
MAX_LIMIT = 2000
_CACHE: dict[tuple[object, ...], tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.RLock()


class FirmsRequestError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def source_metadata() -> dict:
    settings = get_settings()
    configured = bool(settings.nasa_firms_map_key)
    return {
        "configured": configured,
        "sources": [
            {
                "id": source_id,
                **definition,
                "available": True,
                "status": "configured" if configured else "needs_key",
            }
            for source_id, definition in FIRMS_SOURCE_CATALOG.items()
        ],
        "wms_layers": [
            {"id": layer_id, "layer": layer_name, "available": configured}
            for layer_id, layer_name in WMS_LAYERS.items()
        ],
        "cache_ttl_seconds": max(60, settings.nasa_firms_cache_ttl_seconds),
        "attribution": "Data: NASA FIRMS / NASA EOSDIS LANCE",
    }


def validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    values = (west, south, east, north)
    if not all(math.isfinite(value) for value in values):
        raise FirmsRequestError("Bounding box tidak valid", 400)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise FirmsRequestError("Bounding box harus berurutan dan berada dalam rentang koordinat dunia", 400)
    if east - west > MAX_BBOX_SPAN or north - south > MAX_BBOX_SPAN:
        raise FirmsRequestError("Bounding box terlalu luas; zoom ke area analisis terlebih dahulu", 400)
    if (east - west) * (north - south) > MAX_BBOX_AREA:
        raise FirmsRequestError("Luas bounding box terlalu besar untuk query FIRMS", 400)
    return tuple(round(value, 5) for value in values)


def validate_query(
    source: str, day_range: int, requested_date: str | None, min_confidence: str | None, min_frp: float | None, limit: int
) -> tuple[str, int, date | None, float | None, float | None, int]:
    source = source.strip().upper()
    if source != "ALL" and source not in FIRMS_SOURCE_CATALOG:
        raise FirmsRequestError("Source FIRMS tidak valid", 400)
    settings = get_settings()
    allowed_days = {1, 2, 3, 7}
    if day_range not in allowed_days or day_range > settings.nasa_firms_max_day_range:
        raise FirmsRequestError("day_range harus 1, 2, 3, atau 7 hari", 400)
    parsed_date = None
    if requested_date:
        try:
            parsed_date = date.fromisoformat(requested_date)
        except ValueError as exc:
            raise FirmsRequestError("date harus berformat YYYY-MM-DD", 400) from exc
        if parsed_date > datetime.now(UTC).date():
            raise FirmsRequestError("date tidak boleh berada di masa depan", 400)
    confidence = parse_confidence_threshold(min_confidence)
    if min_frp is not None and (not math.isfinite(min_frp) or min_frp < 0):
        raise FirmsRequestError("min_frp harus berupa angka positif", 400)
    if not 1 <= limit <= MAX_LIMIT:
        raise FirmsRequestError(f"limit harus berada pada 1-{MAX_LIMIT}", 400)
    return source, day_range, parsed_date, confidence, min_frp, limit


def parse_confidence_threshold(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().casefold()
    if normalized in {"low", "nominal", "high"}:
        return {"low": 0.0, "nominal": 50.0, "high": 80.0}[normalized]
    try:
        threshold = float(normalized)
    except ValueError as exc:
        raise FirmsRequestError("min_confidence harus angka 0-100 atau low/nominal/high", 400) from exc
    if not 0 <= threshold <= 100:
        raise FirmsRequestError("min_confidence harus berada pada 0-100", 400)
    return threshold


def _number(value: object) -> float | None:
    try:
        result = float(str(value).strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _confidence(value: object) -> tuple[int | float | str | None, float | None, str | None]:
    if value is None or not str(value).strip():
        return None, None, None
    raw = str(value).strip()
    numeric = _number(raw)
    if numeric is not None and 0 <= numeric <= 100:
        normalized = int(numeric) if numeric.is_integer() else numeric
        label = "high" if numeric >= 80 else "nominal" if numeric >= 50 else "low"
        return normalized, numeric, label
    label = {"h": "high", "high": "high", "n": "nominal", "nominal": "nominal", "l": "low", "low": "low"}.get(raw.casefold())
    return raw, None, label


def _acquisition_datetime(acq_date: object, acq_time: object) -> str | None:
    try:
        parsed_date = date.fromisoformat(str(acq_date).strip())
        digits = "".join(ch for ch in str(acq_time or "").strip() if ch.isdigit()).zfill(4)
        hours, minutes = int(digits[:2]), int(digits[2:4])
        if hours > 23 or minutes > 59:
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, hours, minutes, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def normalize_firms_csv(content: str, source: str, near_real_time: bool = True) -> list[dict]:
    if not content.strip():
        return []
    definition = FIRMS_SOURCE_CATALOG[source]
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames or "latitude" not in reader.fieldnames or "longitude" not in reader.fieldnames:
        raise FirmsRequestError("Response FIRMS bukan CSV hotspot yang valid", 502)
    features = []
    for row in reader:
        latitude, longitude = _number(row.get("latitude")), _number(row.get("longitude"))
        if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        confidence, confidence_numeric, confidence_label = _confidence(row.get("confidence"))
        acq_date, acq_time = str(row.get("acq_date") or ""), str(row.get("acq_time") or "").zfill(4)
        acquired_at = _acquisition_datetime(acq_date, acq_time)
        stable_input = "|".join((str(row.get("satellite") or ""), str(row.get("instrument") or ""), acq_date, acq_time, f"{latitude:.5f}", f"{longitude:.5f}"))
        stable_id = f"firms-{hashlib.sha256(stable_input.encode()).hexdigest()[:20]}"
        properties = {
            "latitude": latitude,
            "longitude": longitude,
            "acq_date": acq_date or None,
            "acq_time": acq_time,
            "acq_datetime_utc": acquired_at,
            "satellite": row.get("satellite") or None,
            "instrument": row.get("instrument") or definition["sensor"],
            "confidence": confidence,
            "confidence_numeric": confidence_numeric,
            "confidence_label": confidence_label,
            "frp": _number(row.get("frp")),
            "bright_ti4": _number(row.get("bright_ti4") or row.get("brightness")),
            "bright_ti5": _number(row.get("bright_ti5") or row.get("bright_t31")),
            "scan": _number(row.get("scan")),
            "track": _number(row.get("track")),
            "daynight": row.get("daynight") or None,
            "source": source,
            "source_label": definition["label"],
            "resolution_m": definition["resolution_m"],
            "is_near_real_time": near_real_time,
            "raw": dict(row),
        }
        features.append({"type": "Feature", "id": stable_id, "geometry": {"type": "Point", "coordinates": [longitude, latitude]}, "properties": properties})
    return features


def _base_url() -> str:
    parsed = urlparse(get_settings().nasa_firms_base_url.rstrip("/"))
    if parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST:
        raise FirmsRequestError("NASA_FIRMS_BASE_URL harus menunjuk ke host resmi NASA FIRMS", 500)
    return parsed.geturl().rstrip("/")


def _fetch_source_uncached(source: str, bbox: tuple[float, float, float, float], day_range: int, requested_date: date | None) -> tuple[list[dict], bool, bool]:
    settings = get_settings()
    if not settings.nasa_firms_map_key:
        raise FirmsRequestError("NASA FIRMS belum dikonfigurasi di server", 503)
    cache_date = requested_date.isoformat() if requested_date else "latest"
    cache_key = (source, bbox, cache_date, day_range)
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] <= max(60, settings.nasa_firms_cache_ttl_seconds):
            return [dict(feature) for feature in cached[1]], True, False
    date_part = f"/{requested_date.isoformat()}" if requested_date else ""
    url = f"{_base_url()}/api/area/csv/{settings.nasa_firms_map_key}/{source}/{','.join(str(value) for value in bbox)}/{day_range}{date_part}"
    response = None
    for attempt in range(2):
        try:
            response = requests.get(url, timeout=max(1, settings.nasa_firms_request_timeout_seconds), headers={"User-Agent": "SAVEGEO/1.0"})
            if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(0.2)
                continue
            break
        except requests.Timeout as exc:
            if attempt == 0:
                time.sleep(0.2)
                continue
            raise FirmsRequestError("NASA FIRMS melewati batas waktu", 504) from exc
        except requests.RequestException as exc:
            raise FirmsRequestError("NASA FIRMS tidak dapat dihubungi", 502) from exc
    if response is None:
        raise FirmsRequestError("NASA FIRMS tidak mengembalikan response", 502)
    if response.status_code in {401, 403}:
        raise FirmsRequestError("NASA FIRMS MAP_KEY ditolak atau belum aktif", 502)
    if response.status_code == 429:
        raise FirmsRequestError("Batas rate NASA FIRMS tercapai; coba lagi setelah beberapa saat", 429)
    if response.status_code >= 500:
        raise FirmsRequestError("NASA FIRMS sedang tidak tersedia", 502)
    try:
        response.raise_for_status()
        features = normalize_firms_csv(response.text, source, near_real_time=requested_date is None or requested_date >= datetime.now(UTC).date() - timedelta(days=7))
    except FirmsRequestError:
        raise
    except requests.RequestException as exc:
        raise FirmsRequestError("NASA FIRMS menolak query", 502) from exc
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), features)
    return [dict(feature) for feature in features], False, False


def _fetch_source(source: str, bbox: tuple[float, float, float, float], day_range: int, requested_date: date | None) -> tuple[list[dict], bool, bool]:
    """Fetch fresh data, falling back to an expired entry on upstream failure."""
    try:
        return _fetch_source_uncached(source, bbox, day_range, requested_date)
    except FirmsRequestError:
        cache_date = requested_date.isoformat() if requested_date else "latest"
        cache_key = (source, bbox, cache_date, day_range)
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached:
            return [dict(feature) for feature in cached[1]], True, True
        raise


def _feature_matches(feature: dict, confidence: float | None, min_frp: float | None) -> bool:
    properties = feature.get("properties") or {}
    numeric_confidence = properties.get("confidence_numeric")
    confidence_label = properties.get("confidence_label")
    if confidence is not None and numeric_confidence is not None and numeric_confidence < confidence:
        return False
    if confidence is not None and numeric_confidence is None and confidence_label is not None:
        ranks = {"low": 0, "nominal": 1, "high": 2}
        required = "high" if confidence >= 80 else "nominal" if confidence >= 50 else "low"
        if ranks.get(confidence_label, -1) < ranks[required]:
            return False
    if min_frp is not None and (properties.get("frp") is None or properties["frp"] < min_frp):
        return False
    return True


def _summary(features: list[dict]) -> dict:
    high = [feature for feature in features if (feature.get("properties") or {}).get("confidence_label") == "high"]
    frps = [float((feature.get("properties") or {}).get("frp")) for feature in features if (feature.get("properties") or {}).get("frp") is not None]
    dates = [str((feature.get("properties") or {}).get("acq_date")) for feature in features if (feature.get("properties") or {}).get("acq_date")]
    source_counts = Counter((feature.get("properties") or {}).get("source") for feature in features)
    by_day = Counter(dates)
    return {
        "total_hotspots": len(features),
        "high_confidence_hotspots": len(high),
        "total_frp": round(sum(frps), 3),
        "max_frp": round(max(frps), 3) if frps else 0,
        "latest_detection_utc": max(((feature.get("properties") or {}).get("acq_datetime_utc") for feature in features), default=None),
        "by_day": [{"date": day, "count": count} for day, count in sorted(by_day.items())],
        "by_source": [{"source": source, "count": count} for source, count in sorted(source_counts.items())],
    }


def get_fires(source: str, day_range: int, requested_date: str | None, bbox: tuple[float, float, float, float], min_confidence: str | None, min_frp: float | None, limit: int) -> dict:
    source, day_range, parsed_date, confidence, min_frp, limit = validate_query(source, day_range, requested_date, min_confidence, min_frp, limit)
    bbox = validate_bbox(bbox)
    if not get_settings().nasa_firms_map_key:
        raise FirmsRequestError("NASA FIRMS belum dikonfigurasi di server", 503)
    sources = list(FIRMS_SOURCE_CATALOG) if source == "ALL" else [source]
    features: list[dict] = []
    cached = True
    stale = False
    errors = []
    for source_id in sources:
        try:
            source_features, was_cached, was_stale = _fetch_source(source_id, bbox, day_range, parsed_date)
            cached = cached and was_cached
            stale = stale or was_stale
            features.extend(source_features)
        except FirmsRequestError as exc:
            if source != "ALL":
                raise
            errors.append({"source": source_id, "message": str(exc), "status": exc.status_code})
    filtered = [feature for feature in features if _feature_matches(feature, confidence, min_frp)]
    filtered.sort(key=lambda feature: (feature.get("properties") or {}).get("acq_datetime_utc") or "", reverse=True)
    is_near_real_time = parsed_date is None or parsed_date >= datetime.now(UTC).date() - timedelta(days=7)
    return {
        "type": "FeatureCollection",
        "features": filtered[:limit],
        "metadata": {
            "source": source,
            "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "count": min(len(filtered), limit),
            "raw_count": len(features),
            "is_near_real_time": is_near_real_time,
            "cached": cached,
            "stale": stale,
            "truncated": len(filtered) > limit,
            "errors": errors,
            "summary": _summary(filtered[:limit]),
            "attribution": "Data: NASA FIRMS / NASA EOSDIS LANCE",
            "disclaimer": "Hotspot NASA FIRMS adalah deteksi panas berbasis satelit, bukan batas api atau estimasi luas kebakaran.",
        },
    }


def fetch_wms(layer: str, bbox: tuple[float, float, float, float], crs: str, width: int, height: int, image_format: str) -> dict:
    settings = get_settings()
    if not settings.nasa_firms_map_key:
        raise FirmsRequestError("NASA FIRMS belum dikonfigurasi di server", 503)
    if layer not in WMS_LAYERS:
        raise FirmsRequestError("Layer WMS FIRMS tidak diizinkan", 400)
    if crs not in {"EPSG:4326", "EPSG:3857"}:
        raise FirmsRequestError("CRS WMS hanya mendukung EPSG:4326 atau EPSG:3857", 400)
    if image_format not in {"image/png", "image/jpeg"} or not 64 <= width <= 2048 or not 64 <= height <= 2048:
        raise FirmsRequestError("Parameter WMS tidak valid", 400)
    bbox = validate_bbox(bbox)
    url = f"{_base_url()}/mapserver/wms/fires/{settings.nasa_firms_map_key}/{WMS_LAYERS[layer]}/"
    try:
        response = requests.get(url, params={"BBOX": ",".join(str(value) for value in bbox), "HEIGHT": height, "WIDTH": width, "REQUEST": "GetMap", "SERVICE": "WMS", "VERSION": "1.1.1", "LAYERS": WMS_LAYERS[layer], "SRS": crs, "FORMAT": image_format, "TRANSPARENT": "TRUE"}, timeout=max(1, settings.nasa_firms_request_timeout_seconds))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FirmsRequestError("Layer WMS NASA FIRMS tidak tersedia", 502) from exc
    return {"content_type": response.headers.get("content-type", image_format), "content": response.content, "attribution": "Data: NASA FIRMS / NASA EOSDIS LANCE"}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
