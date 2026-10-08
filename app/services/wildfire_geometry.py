"""Geometry guards for persisted wildfire observations."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from shapely import make_valid
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import PreparedGeometry, prep

KALIMANTAN_GEOMETRY_PATH = (
    Path(__file__).resolve().parents[1] / "static" / "geo" / "islands" / "Kalimantan.geojson"
)
SUMATRA_GEOMETRY_PATH = KALIMANTAN_GEOMETRY_PATH.with_name("Sumatra.geojson")
EVENT_ISLANDS = {"kalimantan-2026": "Kalimantan", "sumatra-2026": "Sumatra"}


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


@lru_cache(maxsize=2)
def _prepared_island(island: str) -> PreparedGeometry | None:
    """Prepare the land mask once, avoiding a polygon scan for every hotspot."""
    if island == "Kalimantan":
        payload = kalimantan_geometry()
    elif island == "Sumatra":
        try:
            payload = json.loads(SUMATRA_GEOMETRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    else:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "FeatureCollection":
        region = unary_union([shape(feature["geometry"]) for feature in payload["features"]])
    else:
        region = shape(payload.get("geometry", payload))
    return prep(make_valid(region))


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
    region = _prepared_island("Kalimantan")
    if region is None:
        return True
    point = _point_from_geojson(geojson)
    return point is not None and region.covers(Point(*point))


def is_sumatra_geometry(geojson: dict[str, Any]) -> bool:
    """Exclude nearby Malaysia/Java and ocean cells from Sumatra hotspots."""
    region = _prepared_island("Sumatra")
    if region is None:
        return True
    point = _point_from_geojson(geojson)
    return point is not None and region.covers(Point(*point))


def is_feature_in_event_scope(event: object, feature: dict[str, Any]) -> bool:
    """Apply the configured island mask to canonical wildfire monitoring events."""
    slug = getattr(event, "slug", None)
    island = EVENT_ISLANDS.get(slug) if isinstance(slug, str) else None
    if island is None:
        return True
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return False
    return is_sumatra_geometry(geometry) if island == "Sumatra" else is_kalimantan_geometry(geometry)
