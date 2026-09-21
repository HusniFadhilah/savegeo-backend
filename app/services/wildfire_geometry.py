"""Geometry guards for persisted wildfire observations."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

KALIMANTAN_GEOMETRY_PATH = (
    Path(__file__).resolve().parents[1] / "static" / "geo" / "islands" / "Kalimantan.geojson"
)


def _ring_contains(point: tuple[float, float], ring: list[Any]) -> bool:
    x, y = point
    inside = False
    if not ring:
        return False
    previous = ring[-1]
    for current in ring:
        if not isinstance(previous, list) or not isinstance(current, list):
            previous = current
            continue
        if len(previous) < 2 or len(current) < 2:
            previous = current
            continue
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def geometry_contains(point: tuple[float, float], geometry: dict[str, Any]) -> bool:
    """Return whether a WGS84 point is inside a GeoJSON geometry."""
    geometry_type = geometry.get("type")
    if geometry_type == "FeatureCollection":
        return any(geometry_contains(point, feature) for feature in geometry.get("features", []))
    if geometry_type == "Feature":
        nested = geometry.get("geometry")
        return isinstance(nested, dict) and geometry_contains(point, nested)
    if geometry_type == "Polygon":
        rings = geometry.get("coordinates") or []
        return bool(rings) and _ring_contains(point, rings[0]) and not any(
            _ring_contains(point, ring) for ring in rings[1:]
        )
    if geometry_type == "MultiPolygon":
        return any(
            geometry_contains(point, {"type": "Polygon", "coordinates": polygon})
            for polygon in geometry.get("coordinates", [])
        )
    return False


@lru_cache(maxsize=1)
def kalimantan_geometry() -> dict[str, Any] | None:
    """Load the checked-in Kalimantan land mask once per process."""
    try:
        payload = json.loads(KALIMANTAN_GEOMETRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _point_from_geojson(geojson: dict[str, Any]) -> tuple[float, float] | None:
    geometry = geojson.get("geometry", geojson)
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        return float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None


def is_kalimantan_geometry(geojson: dict[str, Any]) -> bool:
    """Check a stored hotspot geometry; fail open if the asset is unavailable."""
    region = kalimantan_geometry()
    if region is None:
        return True
    point = _point_from_geojson(geojson)
    return point is not None and geometry_contains(point, region)


def is_feature_in_event_scope(event: object, feature: dict[str, Any]) -> bool:
    """Apply the Kalimantan mask only to the canonical Kalimantan event."""
    if getattr(event, "slug", None) != "kalimantan-2026":
        return True
    geometry = feature.get("geometry")
    return isinstance(geometry, dict) and is_kalimantan_geometry(geometry)
