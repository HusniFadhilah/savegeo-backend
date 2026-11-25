"""Region hierarchy endpoints - /api/regions/*.

Provinces/cities/districts/villages/geometry proxy an external REST API
(REGION_API_BASE_URL, default api.sp3stab.id). Islands and the whole-Indonesia
geometry are served from local static GeoJSON files. The whole-Indonesia AOI
is refreshed from BIG's official BAPANAS/Batas_Administrasi service; island
groupings remain project-specific, not an official administrative level.
Ported from app.py.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from collections import OrderedDict
from threading import Lock, BoundedSemaphore
from pathlib import Path

from app.core.public_http import public_http as requests
from fastapi import APIRouter, HTTPException, Query

from app.core.config import get_settings
from app.services.boundary_provider import CachedBoundaryProvider, RegionBoundaryProvider

router = APIRouter(prefix="/regions", tags=["regions"])
logger = logging.getLogger(__name__)

GEO_STATIC_DIR = Path(__file__).resolve().parents[2] / "static" / "geo"
PROVINCES_38_GEOJSON = GEO_STATIC_DIR / "indonesia_provinces_38.geojson"
ISLANDS_GEO_DIR = GEO_STATIC_DIR / "islands"
ISLAND_NAMES = ["Sumatra", "Kalimantan", "Jawa", "Sulawesi", "Bali_Nusa_Tenggara", "Maluku", "Papua"]
ISLAND_LABELS = {
    "Sumatra": "Sumatra", "Kalimantan": "Kalimantan", "Jawa": "Jawa", "Sulawesi": "Sulawesi",
    "Bali_Nusa_Tenggara": "Bali & Nusa Tenggara", "Maluku": "Maluku", "Papua": "Papua",
}

_CHILD_GEOMETRY_CACHE: OrderedDict = OrderedDict()
_CHILD_CACHE_LOCK = Lock()
_CACHE_TTL_SECONDS = 3600
_REGION_DEADLINE_SECONDS = 25.0
_REGION_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="regions")
_REGION_SLOTS = BoundedSemaphore(24)
_VALIDATED_BOUNDARY_PROVIDER = CachedBoundaryProvider(RegionBoundaryProvider(), ttl_seconds=86400)


def _local_province_features() -> dict[str, dict]:
    """Read the checked-in 38-province boundary set for provider gaps."""
    try:
        payload = json.loads(PROVINCES_38_GEOJSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(feature.get("properties", {}).get("KODE_PROV")): feature
        for feature in payload.get("features", [])
        if feature.get("properties", {}).get("KODE_PROV")
    }


def _swap_lat_lng(node):
    """The upstream region API returns polygon coordinates as
    [lat, lng] pairs for province/city/district - confirmed against live data
    (e.g. Kecamatan Bangli's `meta` center is lat=-8.45/long=115.35, and its
    ring coordinates come back as [-8.52, 115.33, ...], same order). GeoJSON
    (RFC 7946) requires [lng, lat]. Recursively swaps every innermost 2-number
    coordinate pair so every geometry this module returns is spec-compliant
    for any consumer (Leaflet, this frontend, or anything else).

    NOT applied to village/kelurahan geometry - confirmed those already come
    back as [lng, lat] (e.g. Kelurahan Gedawang's ring coordinates are
    [110.4379, -7.075, ...], matching its real long=110.43/lat=-7.09). Swapping
    them would flip already-correct coordinates back into [lat, lng]. See
    `_fix_geometry_coord_order`'s `level` param.
    """
    if isinstance(node, list):
        if len(node) == 2 and all(isinstance(n, (int, float)) for n in node):
            return [node[1], node[0]]
        return [_swap_lat_lng(child) for child in node]
    return node


def _fix_geometry_coord_order(geometry, level: str = ""):
    """Apply `_swap_lat_lng` to a GeoJSON geometry, Feature, or FeatureCollection
    dict in place - skipped when `level == "village"` (already [lng, lat])."""
    if level == "village":
        return geometry
    if not isinstance(geometry, dict):
        return geometry
    gtype = geometry.get("type")
    if gtype == "FeatureCollection":
        for feature in geometry.get("features", []):
            _fix_geometry_coord_order(feature, level)
    elif gtype == "Feature":
        _fix_geometry_coord_order(geometry.get("geometry"), level)
    elif gtype == "GeometryCollection":
        for geom in geometry.get("geometries", []):
            _fix_geometry_coord_order(geom, level)
    elif "coordinates" in geometry:
        geometry["coordinates"] = _swap_lat_lng(geometry["coordinates"])
    return geometry


def _base_url() -> str:
    return get_settings().region_api_base_url


def _classify_province_island(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("sumatera", "sumatra", "aceh", "bangka", "riau", "jambi", "bengkulu", "lampung")):
        return "Sumatra"
    if any(k in n for k in ("kalimantan", "borneo")):
        return "Kalimantan"
    if any(k in n for k in ("jawa", "java", "jakarta", "yogyakarta", "banten")):
        return "Jawa"
    if any(k in n for k in ("sulawesi", "celebes", "gorontalo")):
        return "Sulawesi"
    if any(k in n for k in ("bali", "nusa tenggara")):
        return "Bali_Nusa_Tenggara"
    if any(k in n for k in ("maluku", "molucca")):
        return "Maluku"
    if any(k in n for k in ("papua", "irian")):
        return "Papua"
    return "UNMAPPED"


@router.get("/provinces")
def get_provinces(island: str | None = None):
    local_features = _local_province_features()
    try:
        r = requests.get(f"{_base_url()}/province", params={"is_for_dropdown": 1}, timeout=20, allow_redirects=False)
        r.raise_for_status()
        data = r.json()
        items = _extract_region_dropdown_items(data)
    except HTTPException:
        raise
    except Exception as e:
        if not local_features:
            logger.error("Region provider request failed")
            raise HTTPException(status_code=502, detail="Region provider temporarily unavailable") from e
        logger.warning("Using checked-in province list while the region provider is unavailable")
        items = []
    merged = {code: name for name, code in items}
    for code, feature in local_features.items():
        # The external provider still exposes old, unsplit Papua boundaries for
        # some codes. Prefer the checked-in 38-province set for names as well as
        # geometry so dropdown codes cannot combine old and new administrations.
        merged[code] = str(feature.get("properties", {}).get("PROVINSI") or merged.get(code) or code)
    if island:
        if island not in ISLAND_NAMES:
            raise HTTPException(status_code=400, detail=f"Unknown island {island!r}. Valid: {ISLAND_NAMES}")
        merged = {code: name for code, name in merged.items() if _classify_province_island(name) == island}
    return {name: code for code, name in sorted(merged.items(), key=lambda item: item[1])}


@router.get("/cities")
def get_cities(province_code: str = ""):
    if not province_code:
        raise HTTPException(status_code=400, detail="province_code is required")
    try:
        r = requests.get(f"{_base_url()}/city", params={"is_for_dropdown": 1, "parent_code": province_code}, timeout=20, allow_redirects=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("Region provider request failed")
        raise HTTPException(status_code=502, detail="Region provider temporarily unavailable") from e


@router.get("/provinces/{code}/geometry")
def get_province_geometry(code: str):
    """Stable internal boundary contract for the wildfire module.

    External SP3STAB response details stay behind this adapter; callers never
    pass an external URL or need to know the legacy ``endpoint`` parameter.
    """
    return _get_validated_geometry("province", code)


@router.get("/provinces/{code}/cities")
def get_province_cities(code: str):
    return get_cities(province_code=code)


@router.get("/cities/{code}/geometry")
def get_city_geometry(code: str):
    return _get_validated_geometry("city", code)


def _get_validated_geometry(endpoint: str, code: str):
    try:
        return _VALIDATED_BOUNDARY_PROVIDER.geometry(endpoint, code)[0]
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Boundary geometry validation failed") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Boundary provider temporarily unavailable") from exc


@router.get("/districts")
def get_districts(city_code: str = ""):
    if not city_code:
        raise HTTPException(status_code=400, detail="city_code is required")
    try:
        r = requests.get(f"{_base_url()}/district", params={"is_for_dropdown": 1, "parent_code": city_code}, timeout=20, allow_redirects=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("Region provider request failed")
        raise HTTPException(status_code=502, detail="Region provider temporarily unavailable") from e


@router.get("/villages")
def get_villages(district_code: str = ""):
    if not district_code:
        raise HTTPException(status_code=400, detail="district_code is required")
    try:
        r = requests.get(f"{_base_url()}/village", params={"is_for_dropdown": 1, "parent_code": district_code}, timeout=20, allow_redirects=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("Region provider request failed")
        raise HTTPException(status_code=502, detail="Region provider temporarily unavailable") from e


@router.get("/geometry")
def get_region_geometry(endpoint: str = "", code: str = Query(default="", min_length=1, max_length=32, pattern=r"^[0-9.]+$")):
    if not endpoint or not code:
        raise HTTPException(status_code=400, detail="endpoint and code are required")
    if endpoint not in {"province", "city", "district", "village"}:
        raise HTTPException(status_code=400, detail="Invalid region endpoint")
    if endpoint == "province":
        local = _local_province_features().get(code)
        if local:
            return local
    try:
        r = requests.get(f"{_base_url()}/{endpoint}", params={"code": code}, timeout=20, allow_redirects=False)
        r.raise_for_status()
        data = r.json()
        if data.get("meta", {}).get("code") == 200:
            region = data.get("data", {}).get("region")
            if isinstance(region, dict) and region.get("type") == "FeatureCollection" and not region.get("features"):
                local = _local_province_features().get(code) if endpoint == "province" else None
                if local:
                    logger.warning("Using checked-in province boundary for empty provider geometry code=%s", code)
                    return local
            return _fix_geometry_coord_order(region, endpoint)
        raise HTTPException(status_code=404, detail="Region not found")
    except HTTPException:
        local = _local_province_features().get(code) if endpoint == "province" else None
        if local:
            return local
        raise
    except Exception as e:
        local = _local_province_features().get(code) if endpoint == "province" else None
        if local:
            logger.warning("Using checked-in province boundary for provider gap code=%s", code)
            return local
        logger.error("Region provider request failed")
        raise HTTPException(status_code=502, detail="Region provider temporarily unavailable") from e


def _extract_region_dropdown_items(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload, dict):
        return [(str(name), str(code)) for name, code in payload.items()]
    if isinstance(payload, list):
        items = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("label") or item.get("nama")
            code = item.get("code") or item.get("value") or item.get("kode")
            if name and code:
                items.append((str(name), str(code)))
        return items
    return []


def _get_cached_children_geometries(cache_key):
    with _CHILD_CACHE_LOCK:
        item = _CHILD_GEOMETRY_CACHE.get(cache_key)
        if not item:
            return None
        created_at, payload = item
        if time.time() - created_at > _CACHE_TTL_SECONDS:
            _CHILD_GEOMETRY_CACHE.pop(cache_key, None)
            return None
        _CHILD_GEOMETRY_CACHE.move_to_end(cache_key)
        return payload


def _cache_children_geometries(cache_key, payload):
    with _CHILD_CACHE_LOCK:
        _CHILD_GEOMETRY_CACHE[cache_key] = (time.time(), payload)
        _CHILD_GEOMETRY_CACHE.move_to_end(cache_key)
        while len(_CHILD_GEOMETRY_CACHE) > 256:
            _CHILD_GEOMETRY_CACHE.popitem(last=False)


def _fetch_region_child_geometry(base_url, child_endpoint, name, code, deadline=None):
    remaining = (deadline - time.monotonic()) if deadline is not None else 10
    if remaining <= 0:
        raise TimeoutError("Region request deadline exceeded")
    geom_res = requests.get(f"{base_url}/{child_endpoint}", params={"code": code}, timeout=(min(3, remaining), min(10, remaining)), allow_redirects=False)
    geom_res.raise_for_status()
    payload = geom_res.json()
    region = payload.get("data", {}).get("region") if isinstance(payload, dict) else None
    if not region:
        return [], {"name": name, "code": code, "reason": "empty geometry"}
    region = _fix_geometry_coord_order(region, child_endpoint)

    if region.get("type") == "FeatureCollection":
        region_features = region.get("features", [])
    elif region.get("type") == "Feature":
        region_features = [region]
    else:
        region_features = [{"type": "Feature", "geometry": region, "properties": {}}]

    features = []
    for feature in region_features:
        props = feature.setdefault("properties", {})
        props.update({"name": name, "code": code, "level": child_endpoint})
        features.append(feature)
    return features, None


def _normalize_region_feature_collection(payload, child_endpoint, limit):
    region = payload.get("data", {}).get("region") if isinstance(payload, dict) else None
    if not isinstance(region, dict):
        return None
    region = _fix_geometry_coord_order(region, child_endpoint)

    if region.get("type") == "FeatureCollection":
        features = list(region.get("features") or [])
    elif region.get("type") == "Feature":
        features = [region]
    elif region.get("type") in {"Polygon", "MultiPolygon", "GeometryCollection"}:
        features = [{"type": "Feature", "geometry": region, "properties": {}}]
    else:
        return None

    if limit and limit > 0:
        features = features[:limit]

    normalized = []
    for feature in features:
        if not isinstance(feature, dict) or not feature.get("geometry"):
            continue
        props = feature.setdefault("properties", {})
        props["name"] = props.get("name") or props.get("NAMOBJ") or props.get("nama") or props.get("code") or "Region"
        props["code"] = str(props.get("code") or props.get("kode") or props.get("id") or "")
        props["level"] = child_endpoint
        normalized.append(feature)

    return normalized


_ENDPOINT_TO_COLUMN = {
    "province": "province_code",
    "city": "city_code",
    "district": "district_code",
}


@router.get("/children-geometries")
def get_region_children_geometries(
    parent_code: str = Query(default="", min_length=1, max_length=32, pattern=r"^[0-9.]+$"),
    child_endpoint: str = "",
    parent_endpoint: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
):
    if not parent_code or not child_endpoint:
        raise HTTPException(status_code=400, detail="parent_code and child_endpoint are required")
    if child_endpoint not in {"city", "district", "village"}:
        raise HTTPException(status_code=400, detail="child_endpoint must be city, district, or village")

    parent_column = _ENDPOINT_TO_COLUMN.get(parent_endpoint or "")
    base_url = _base_url()
    cache_key = (base_url, parent_endpoint or "", parent_code, child_endpoint, limit)
    deadline = time.monotonic() + _REGION_DEADLINE_SECONDS

    def timeout_budget():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPException(status_code=504, detail="Region request deadline exceeded")
        return min(3, remaining), min(10, remaining)

    try:
        cached_payload = _get_cached_children_geometries(cache_key)
        if cached_payload:
            cached_payload = dict(cached_payload)
            cached_payload.setdefault("meta", {})
            cached_payload["meta"] = {**cached_payload["meta"], "cached": True}
            return cached_payload

        if parent_column:
            bulk_res = requests.get(
                f"{base_url}/{child_endpoint}",
                params={"parent_column": parent_column, "parent_code": parent_code},
                timeout=timeout_budget(),
                allow_redirects=False,
            )
            if bulk_res.ok:
                bulk_features = _normalize_region_feature_collection(bulk_res.json(), child_endpoint, limit)
                if bulk_features:
                    response_payload = {
                        "type": "FeatureCollection",
                        "features": bulk_features,
                        "meta": {
                            "parent_code": parent_code,
                            "parent_column": parent_column,
                            "child_endpoint": child_endpoint,
                            "count": len(bulk_features),
                            "failed_count": 0,
                            "failed": [],
                            "cached": False,
                            "source": "bulk_parent_column",
                        },
                    }
                    _cache_children_geometries(cache_key, response_payload)
                    return response_payload

        dropdown_params = {"is_for_dropdown": 1, "parent_code": parent_code}
        if parent_column:
            dropdown_params["parent_column"] = parent_column
        dropdown = requests.get(f"{base_url}/{child_endpoint}", params=dropdown_params, timeout=timeout_budget(), allow_redirects=False)
        dropdown.raise_for_status()

        child_items = _extract_region_dropdown_items(dropdown.json())
        if limit and limit > 0:
            child_items = child_items[:min(limit, 500)]
        features = []
        failed = []

        futures = []
        # Admission is bounded globally per worker, including queued tasks.
        # More children are fetched in subsequent batches while budget remains.
        pending_items = iter(child_items)
        exhausted = False
        while not exhausted and time.monotonic() < deadline:
            batch: list[Future] = []
            while len(batch) < 8:
                try:
                    name, code = next(pending_items)
                except StopIteration:
                    exhausted = True
                    break
                if not _REGION_SLOTS.acquire(blocking=False):
                    raise HTTPException(status_code=503, detail="Region provider busy", headers={"Retry-After": "5"})
                try:
                    task = _REGION_POOL.submit(_fetch_region_child_geometry, base_url, child_endpoint, name, code, deadline)
                except Exception:
                    _REGION_SLOTS.release()
                    raise
                task.add_done_callback(lambda _task: _REGION_SLOTS.release())
                batch.append(task)
                futures.append(task)
            try:
                completed = list(as_completed(batch, timeout=max(0.001, deadline - time.monotonic())))
            except FuturesTimeout:
                for task in batch:
                    task.cancel()
                raise HTTPException(status_code=504, detail="Region request deadline exceeded") from None
            for future in completed:
                try:
                    child_features, child_failed = future.result()
                    features.extend(child_features)
                    if child_failed:
                        failed.append(child_failed)
                except Exception as child_error:  # noqa: BLE001 - one child failing shouldn't abort the batch; collected in `failed`
                    logger.warning("Region child geometry request failed: %s", type(child_error).__name__)
                    failed.append({"name": "unknown", "code": "unknown", "reason": "Boundary provider temporarily unavailable"})

        if not exhausted:
            for task in futures:
                task.cancel()
            raise HTTPException(status_code=504, detail="Region request deadline exceeded")
        response_payload = {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "parent_code": parent_code,
                "child_endpoint": child_endpoint,
                "count": len(features),
                "failed_count": len(failed),
                "failed": failed[:20],
                "cached": False,
                "source": "per_child_fallback",
            },
        }
        _cache_children_geometries(cache_key, response_payload)
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Region provider request failed")
        raise HTTPException(status_code=502, detail="Region provider temporarily unavailable") from e


@router.get("/islands")
def get_islands():
    return [{"name": name, "label": ISLAND_LABELS[name]} for name in ISLAND_NAMES]


@router.get("/islands/{island_name}/geometry")
def get_island_geometry(island_name: str):
    if island_name not in ISLAND_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown island {island_name!r}. Valid: {ISLAND_NAMES}")
    path = ISLANDS_GEO_DIR / f"{island_name}.geojson"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Geometry file missing for {island_name!r}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@router.get("/indonesia/geometry")
def get_indonesia_geometry():
    path = GEO_STATIC_DIR / "indonesia_whole.geojson"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Whole-Indonesia geometry file missing")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
