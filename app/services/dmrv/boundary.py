"""Project boundary validation, geodesic area and overlap summary.

Pure functions (no database). Geometry stays GeoJSON in EPSG:4326; area is the
geodesic area on the WGS84 ellipsoid (pyproj), not a projected planar area.
Invalid geometry is rejected, never silently repaired: the reviewer must see
exactly what the project drew.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pyproj import Geod
from shapely.errors import ShapelyError
from shapely.geometry import Polygon, mapping, shape

# Pilot guard against accidental country-sized polygons; adjust with the carbon expert.
MAX_BOUNDARY_AREA_HA = 500_000.0
AREA_METHOD = "geodesic_wgs84_pyproj"
_OVERLAP_MIN_M2 = 1.0  # ignore float-precision slivers along shared edges

_GEOD = Geod(ellps="WGS84")


class BoundaryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BoundaryValidation:
    validated_geojson: dict
    area_ha: float
    area_method: str
    checksum: str
    hole_count: int
    warnings: tuple[str, ...]


def _geometry_of(geojson: Any) -> dict:
    if not isinstance(geojson, dict):
        raise BoundaryError("unsupported_type", "GeoJSON harus berupa objek.")
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        features = geojson.get("features") or []
        if not features:
            raise BoundaryError("empty", "FeatureCollection tidak berisi fitur.")
        if len(features) > 1:
            raise BoundaryError("too_many_features", "Batas proyek harus satu geometri.")
        return _geometry_of(features[0])
    if kind == "Feature":
        return _geometry_of(geojson.get("geometry"))
    if kind not in ("Polygon", "MultiPolygon"):
        raise BoundaryError("unsupported_type", "Hanya Polygon atau MultiPolygon yang didukung.")
    if not geojson.get("coordinates"):
        raise BoundaryError("empty", "Geometri tidak memiliki koordinat.")
    return geojson


def _clean_position(position: Any) -> list[float]:
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise BoundaryError("bad_coordinates", "Posisi harus berisi minimal lon dan lat.")
    try:
        lon, lat = float(position[0]), float(position[1])
    except (TypeError, ValueError) as exc:
        raise BoundaryError("bad_coordinates", "Koordinat harus numerik.") from exc
    if not (math.isfinite(lon) and math.isfinite(lat)) or not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
        raise BoundaryError("bad_coordinates", "Koordinat di luar rentang EPSG:4326 (lon -180..180, lat -90..90).")
    return [lon, lat]


def _clean_rings(rings: Any) -> list[list[list[float]]]:
    if not isinstance(rings, (list, tuple)) or not rings:
        raise BoundaryError("empty", "Polygon tidak memiliki ring.")
    cleaned = []
    for ring in rings:
        if not isinstance(ring, (list, tuple)):
            raise BoundaryError("bad_coordinates", "Ring harus berupa daftar posisi.")
        points = [_clean_position(position) for position in ring]
        for first, second in zip(points, points[1:]):
            if abs(first[0] - second[0]) > 180:
                raise BoundaryError("antimeridian", "Ring melintasi antimeridian; pisahkan menjadi MultiPolygon.")
        cleaned.append(points)
    return cleaned


def _normalised(geometry: dict) -> dict:
    if geometry["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": _clean_rings(geometry["coordinates"])}
    return {"type": "MultiPolygon", "coordinates": [_clean_rings(polygon) for polygon in geometry["coordinates"]]}


def _ring_area_m2(ring: Any) -> float:
    return abs(_GEOD.geometry_area_perimeter(Polygon(ring))[0])


def _area_m2(geometry: Any) -> float:
    """Geodesic area: exterior minus holes. pyproj's own hole handling depends on
    ring orientation (same-orientation holes get added), so rings are measured
    individually and the sign is applied here."""
    if geometry.geom_type == "Polygon":
        return _ring_area_m2(geometry.exterior) - sum(_ring_area_m2(ring) for ring in geometry.interiors)
    if hasattr(geometry, "geoms"):
        return sum(_area_m2(part) for part in geometry.geoms)
    return 0.0  # points and lines have no area


def validate_boundary(geojson: dict) -> BoundaryValidation:
    normalised = _normalised(_geometry_of(geojson))
    try:
        geometry = shape(normalised)
    except (ShapelyError, ValueError, TypeError) as exc:
        raise BoundaryError("invalid_geometry", "Geometri tidak dapat dibentuk.") from exc
    if geometry.is_empty:
        raise BoundaryError("empty", "Geometri kosong.")
    if not geometry.is_valid:
        raise BoundaryError("invalid_geometry", "Geometri tidak valid (mis. self-intersection).")

    area_ha = _area_m2(geometry) / 10_000
    if area_ha > MAX_BOUNDARY_AREA_HA:
        raise BoundaryError("too_large", f"Luas {area_ha:,.0f} ha melebihi batas {MAX_BOUNDARY_AREA_HA:,.0f} ha.")

    validated = json.loads(json.dumps(mapping(geometry)))
    polygons = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
    canonical = json.dumps(validated, sort_keys=True, separators=(",", ":"))
    return BoundaryValidation(
        validated_geojson=validated,
        area_ha=area_ha,
        area_method=AREA_METHOD,
        checksum=hashlib.sha256(canonical.encode()).hexdigest(),
        hole_count=sum(len(polygon.interiors) for polygon in polygons),
        warnings=(),
    )


def overlap_summary(candidate: dict, others: Iterable[dict]) -> tuple[int, float]:
    """Count of ``others`` overlapping ``candidate`` with positive area, and the
    summed geodesic overlap in ha. Takes geometries only, so callers cannot
    leak the identity of other projects through this function."""
    base = shape(candidate)
    count, total_m2 = 0, 0.0
    for other in others:
        intersection = base.intersection(shape(other))
        if intersection.is_empty:
            continue
        overlap_m2 = _area_m2(intersection)
        if overlap_m2 > _OVERLAP_MIN_M2:
            count += 1
            total_m2 += overlap_m2
    return count, total_m2 / 10_000
