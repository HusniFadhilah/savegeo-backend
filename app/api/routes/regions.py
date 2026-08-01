"""Region hierarchy endpoints - /api/regions/*.

Provinces/cities/districts/villages/geometry proxy an external REST API
(REGION_API_BASE_URL, default api.sp3stab.id). Islands and the whole-Indonesia
geometry are served from local static GeoJSON files (project-specific island
groupings, not an official administrative level). Ported from app.py.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException

from app.core.config import get_settings

router = APIRouter(prefix="/regions", tags=["regions"])

GEO_STATIC_DIR = Path(__file__).resolve().parents[2] / "static" / "geo"
ISLANDS_GEO_DIR = GEO_STATIC_DIR / "islands"
ISLAND_NAMES = ["Sumatra", "Kalimantan", "Jawa", "Sulawesi", "Bali_Nusa_Tenggara", "Maluku", "Papua"]
ISLAND_LABELS = {
    "Sumatra": "Sumatra", "Kalimantan": "Kalimantan", "Jawa": "Jawa", "Sulawesi": "Sulawesi",
    "Bali_Nusa_Tenggara": "Bali & Nusa Tenggara", "Maluku": "Maluku", "Papua": "Papua",
}

_CHILD_GEOMETRY_CACHE: dict = {}
_CACHE_TTL_SECONDS = 3600


def _swap_lat_lng(node):
    """The upstream region API (api.sp3stab.id) returns polygon coordinates as
    [lat, lng] pairs - confirmed against live data (e.g. Kecamatan Bangli's
    `meta` center is lat=-8.45/long=115.35, and its ring coordinates come back
    as [-8.52, 115.33, ...], same order). GeoJSON (RFC 7946) requires
    [lng, lat]. Recursively swaps every innermost 2-number coordinate pair so
    every geometry this module returns is spec-compliant for any consumer
    (Leaflet, this frontend, or anything else)."""
    if isinstance(node, list):
        if len(node) == 2 and all(isinstance(n, (int, float)) for n in node):
            return [node[1], node[0]]
        return [_swap_lat_lng(child) for child in node]
    return node


def _fix_geometry_coord_order(geometry):
    """Apply `_swap_lat_lng` to a GeoJSON geometry, Feature, or FeatureCollection dict in place."""
    if not isinstance(geometry, dict):
        return geometry
    gtype = geometry.get("type")
    if gtype == "FeatureCollection":
        for feature in geometry.get("features", []):
            _fix_geometry_coord_order(feature)
    elif gtype == "Feature":
        _fix_geometry_coord_order(geometry.get("geometry"))
    elif gtype == "GeometryCollection":
        for geom in geometry.get("geometries", []):
            _fix_geometry_coord_order(geom)
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
def get_provinces(island: Optional[str] = None):
    try:
        r = requests.get(f"{_base_url()}/province", params={"is_for_dropdown": 1}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if island:
            if island not in ISLAND_NAMES:
                raise HTTPException(status_code=400, detail=f"Unknown island {island!r}. Valid: {ISLAND_NAMES}")
            if isinstance(data, dict):
                data = {name: code for name, code in data.items() if _classify_province_island(name) == island}
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cities")
def get_cities(province_code: str = ""):
    if not province_code:
        raise HTTPException(status_code=400, detail="province_code is required")
    try:
        r = requests.get(f"{_base_url()}/city", params={"is_for_dropdown": 1, "parent_code": province_code}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/districts")
def get_districts(city_code: str = ""):
    if not city_code:
        raise HTTPException(status_code=400, detail="city_code is required")
    try:
        r = requests.get(f"{_base_url()}/district", params={"is_for_dropdown": 1, "parent_code": city_code}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/villages")
def get_villages(district_code: str = ""):
    if not district_code:
        raise HTTPException(status_code=400, detail="district_code is required")
    try:
        r = requests.get(f"{_base_url()}/village", params={"is_for_dropdown": 1, "parent_code": district_code}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/geometry")
def get_region_geometry(endpoint: str = "", code: str = ""):
    if not endpoint or not code:
        raise HTTPException(status_code=400, detail="endpoint and code are required")
    try:
        r = requests.get(f"{_base_url()}/{endpoint}", params={"code": code}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("meta", {}).get("code") == 200:
            region = data.get("data", {}).get("region")
            return _fix_geometry_coord_order(region)
        raise HTTPException(status_code=404, detail="Region not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    item = _CHILD_GEOMETRY_CACHE.get(cache_key)
    if not item:
        return None
    created_at, payload = item
    if time.time() - created_at > _CACHE_TTL_SECONDS:
        _CHILD_GEOMETRY_CACHE.pop(cache_key, None)
        return None
    return payload


def _fetch_region_child_geometry(base_url, child_endpoint, name, code):
    geom_res = requests.get(f"{base_url}/{child_endpoint}", params={"code": code}, timeout=20)
    geom_res.raise_for_status()
    payload = geom_res.json()
    region = payload.get("data", {}).get("region") if isinstance(payload, dict) else None
    if not region:
        return [], {"name": name, "code": code, "reason": "empty geometry"}
    region = _fix_geometry_coord_order(region)

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
    region = _fix_geometry_coord_order(region)

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
    parent_code: str = "",
    child_endpoint: str = "",
    parent_endpoint: Optional[str] = None,
    limit: int = 10000,
):
    if not parent_code or not child_endpoint:
        raise HTTPException(status_code=400, detail="parent_code and child_endpoint are required")
    if child_endpoint not in {"city", "district", "village"}:
        raise HTTPException(status_code=400, detail="child_endpoint must be city, district, or village")

    parent_column = _ENDPOINT_TO_COLUMN.get(parent_endpoint or "")
    base_url = _base_url()
    cache_key = (base_url, parent_endpoint or "", parent_code, child_endpoint, limit)

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
                timeout=60,
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
                    _CHILD_GEOMETRY_CACHE[cache_key] = (time.time(), response_payload)
                    return response_payload

        dropdown_params = {"is_for_dropdown": 1, "parent_code": parent_code}
        if parent_column:
            dropdown_params["parent_column"] = parent_column
        dropdown = requests.get(f"{base_url}/{child_endpoint}", params=dropdown_params, timeout=30)
        dropdown.raise_for_status()

        child_items = _extract_region_dropdown_items(dropdown.json())
        if limit and limit > 0:
            child_items = child_items[:limit]
        features = []
        failed = []

        max_workers = min(12, max(1, len(child_items)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_fetch_region_child_geometry, base_url, child_endpoint, name, code)
                for name, code in child_items
            ]
            for future in as_completed(futures):
                try:
                    child_features, child_failed = future.result()
                    features.extend(child_features)
                    if child_failed:
                        failed.append(child_failed)
                except Exception as child_error:
                    failed.append({"name": "unknown", "code": "unknown", "reason": str(child_error)})

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
        _CHILD_GEOMETRY_CACHE[cache_key] = (time.time(), response_payload)
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
