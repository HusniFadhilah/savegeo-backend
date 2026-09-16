"""Safe administrative-boundary adapters used by the wildfire explorer.

The provider owns the external response shape and axis-order decision. The
frontend only receives validated GeoJSON and never receives an arbitrary URL.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from app.core.config import get_settings

INDONESIA_BBOX = (94.0, -12.0, 142.0, 8.0)


def _walk_coordinates(node: Any, swap: bool = False):
    if isinstance(node, list) and len(node) == 2 and all(isinstance(value, (int, float)) for value in node):
        return [node[1], node[0]] if swap else [node[0], node[1]]
    if isinstance(node, list):
        return [_walk_coordinates(child, swap) for child in node]
    return node


def _points(geometry: dict) -> list[list[float]]:
    coords = geometry.get("coordinates", [])
    found: list[list[float]] = []

    def visit(node):
        if (
            isinstance(node, list)
            and len(node) == 2
            and all(isinstance(value, (int, float)) for value in node)
        ):
            found.append(node)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(coords)
    return found


def _in_indonesia(points: list[list[float]]) -> bool:
    return (
        bool(points)
        and sum(
            INDONESIA_BBOX[0] <= p[0] <= INDONESIA_BBOX[2] and INDONESIA_BBOX[1] <= p[1] <= INDONESIA_BBOX[3]
            for p in points
        )
        / len(points)
        > 0.8
    )


def _closed_rings(geometry: dict) -> bool:
    def rings(node):
        if (
            isinstance(node, list)
            and node
            and isinstance(node[0], list)
            and len(node[0]) == 2
            and isinstance(node[0][0], (int, float))
        ):
            yield node
        elif isinstance(node, list):
            for child in node:
                yield from rings(child)

    return all(len(ring) >= 4 and ring[0] == ring[-1] for ring in rings(geometry.get("coordinates", [])))


@dataclass
class ValidatedGeometry:
    geojson: dict
    validation_status: str
    transformation: str
    checksum: str


def normalize_geometry(payload: dict, *, provider_axis: str = "lat-lng") -> ValidatedGeometry:
    """Normalize a Feature/FeatureCollection from SP3STAB.

    SP3STAB province/city geometry is configured as latitude-longitude. The
    rule is provider-level and applies to the complete geometry, never one
    coordinate at a time. A fixture or an explicit provider configuration can
    set ``provider_axis=lng-lat`` for a source already conforming to GeoJSON.
    """
    raw = copy.deepcopy(payload)
    if not isinstance(raw, dict):
        raise ValueError("geometry payload must be an object")
    gtype = raw.get("type")
    if gtype not in {"Feature", "FeatureCollection", "Polygon", "MultiPolygon", "GeometryCollection"}:
        raise ValueError("unsupported geometry type")
    if gtype == "FeatureCollection":
        features = raw.get("features")
        if not isinstance(features, list):
            raise ValueError("FeatureCollection.features must be a list")
        for feature in features:
            if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
                raise ValueError("feature geometry is missing")
        geometries = [feature["geometry"] for feature in features]
    elif gtype == "Feature":
        geometries = [raw.get("geometry")]
    else:
        geometries = [raw]
    if any(geometry is None for geometry in geometries):
        raise ValueError("geometry is missing")
    swap = provider_axis == "lat-lng"
    normalized = copy.deepcopy(raw)
    if gtype == "FeatureCollection":
        for feature in normalized["features"]:
            feature["geometry"]["coordinates"] = _walk_coordinates(
                feature["geometry"].get("coordinates", []), swap
            )
    elif gtype == "Feature":
        normalized["geometry"]["coordinates"] = _walk_coordinates(
            normalized["geometry"].get("coordinates", []), swap
        )
    else:
        normalized["coordinates"] = _walk_coordinates(normalized.get("coordinates", []), swap)
    normalized_points = [
        point
        for geometry in (
            [f["geometry"] for f in normalized.get("features", [])]
            if gtype == "FeatureCollection"
            else [normalized.get("geometry", normalized)]
        )
        for point in _points(geometry)
    ]
    if not _in_indonesia(normalized_points):
        raise ValueError("normalized geometry is outside Indonesia")
    if not all(
        _closed_rings(geometry)
        for geometry in (
            [f["geometry"] for f in normalized.get("features", [])]
            if gtype == "FeatureCollection"
            else [normalized.get("geometry", normalized)]
        )
    ):
        raise ValueError("polygon ring is not closed")
    raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return ValidatedGeometry(
        normalized,
        "valid",
        "lat-lng to lng-lat" if swap else "none",
        hashlib.sha256(raw_json.encode()).hexdigest(),
    )


class Sp3StabBoundaryProvider:
    name = "SP3STAB"

    def __init__(self, base_url: str | None = None, timeout: int = 15):
        self.base_url = (base_url or get_settings().region_api_base_url).rstrip("/")
        self.timeout = timeout

    def dropdown(self, level: str, parent_code: str | None = None) -> dict:
        if level not in {"province", "city"}:
            raise ValueError("unsupported boundary level")
        params = {"is_for_dropdown": 1}
        if parent_code:
            params["parent_code"] = parent_code
        response = requests.get(f"{self.base_url}/{level}", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def geometry(self, level: str, code: str, parent_code: str | None = None) -> dict:
        if level not in {"province", "city"} or not code:
            raise ValueError("unsupported boundary request")
        params = {"code": code, "is_dropdown": 0}
        if parent_code:
            params["parent_code"] = parent_code
        response = requests.get(f"{self.base_url}/{level}", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        region = payload.get("data", {}).get("region") if isinstance(payload, dict) else None
        if not region:
            raise ValueError("provider returned no geometry")
        return normalize_geometry(region).geojson


class CachedBoundaryProvider:
    def __init__(self, provider: Sp3StabBoundaryProvider, ttl_seconds: int = 86400):
        self.provider = provider
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, dict]] = {}

    def geometry(self, level: str, code: str, parent_code: str | None = None) -> tuple[dict, bool]:
        key = f"{level}:{code}:{parent_code or ''}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] <= self.ttl_seconds:
            return cached[1], False
        try:
            payload = self.provider.geometry(level, code, parent_code)
            self._cache[key] = (time.time(), payload)
            return payload, False
        except Exception:
            if cached:
                return cached[1], True
            raise
