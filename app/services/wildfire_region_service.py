"""Spatial filtering and aggregation of wildfire observations by admin area."""

from __future__ import annotations

from typing import Any

from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
from shapely.validation import make_valid


def _valid_shape(geometry: dict[str, Any]):
    """Convert a GeoJSON geometry, Feature, or FeatureCollection to one shape."""
    geometry_type = geometry.get("type")
    if geometry_type == "Feature":
        geometry = geometry.get("geometry") or {}
    elif geometry_type == "FeatureCollection":
        pieces = [
            shape(feature["geometry"])
            for feature in geometry.get("features", [])
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        ]
        if not pieces:
            raise ValueError("FeatureCollection has no valid geometries")
        result = unary_union(pieces)
        return result if result.is_valid else make_valid(result)

    if not isinstance(geometry, dict):
        raise ValueError("GeoJSON geometry is missing")
    result = shape(geometry)
    return result if result.is_valid else make_valid(result)


def filter_features_by_boundary(features: list[dict], geometry: dict[str, Any]) -> list[dict]:
    """Filter hotspot points while preparing the boundary only once."""
    try:
        boundary = prep(_valid_shape(geometry))
    except Exception as exc:
        raise ValueError("Batas wilayah tidak dapat diproses") from exc
    filtered = []
    for feature in features:
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            point = Point(float(coordinates[0]), float(coordinates[1]))
        except (TypeError, ValueError):
            continue
        if boundary.covers(point):
            filtered.append(feature)
    return filtered


def summarize_by_region(features: list[dict], region_features: list[dict]) -> list[dict[str, Any]]:
    """Count filtered hotspot observations in each child administrative unit."""
    groups: dict[tuple[str, str], list[Any]] = {}
    for feature in region_features:
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        properties = feature.get("properties") or {}
        code = str(properties.get("code") or properties.get("kode") or "")
        name = str(properties.get("name") or properties.get("nama") or code or "Wilayah")
        try:
            groups.setdefault((code, name), []).append(_valid_shape(geometry))
        except Exception:  # malformed upstream geometry is excluded from counts
            continue

    regions: list[dict[str, Any]] = []
    geometries = []
    for (code, name), pieces in groups.items():
        try:
            merged = unary_union(pieces)
            if merged.is_empty:
                continue
        except Exception:
            continue
        regions.append({"code": code, "name": name, "hotspot_count": 0, "high_confidence_count": 0})
        geometries.append(merged)

    if not geometries:
        return []

    tree = STRtree(geometries)
    for feature in features:
        coordinates = (feature.get("geometry") or {}).get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            point = Point(float(coordinates[0]), float(coordinates[1]))
            matches = tree.query(point, predicate="covered_by")
        except (TypeError, ValueError):
            continue
        properties = feature.get("properties") or {}
        high = str(properties.get("confidence_label", properties.get("confidence_category", ""))).casefold() == "high"
        # Administrative polygons can both cover a point exactly on their
        # shared border. Count that observation once so child totals do not
        # exceed the number of hotspot detections.
        if len(matches):
            region = regions[min(int(index) for index in matches)]
            region["hotspot_count"] += 1
            if high:
                region["high_confidence_count"] += 1

    return sorted(regions, key=lambda region: (-region["hotspot_count"], region["name"].casefold()))
