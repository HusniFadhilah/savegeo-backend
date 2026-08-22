"""Pure geo helper functions with no DB/GEE dependency."""
from __future__ import annotations

import math


def estimate_area_ha(geojson_obj: dict) -> float:
    """Approximate polygon area in hectares via shoelace formula with a
    latitude-corrected degrees-to-meters conversion. Ported verbatim from
    legacy `backend/admin_routes.py::_estimate_area_ha`. Not geodesically
    exact (a PostGIS `ST_Area(geography)` would be), but matches the
    precision the legacy admin panel has always shown for company boundaries.
    """

    def ring_area_ha(coords: list) -> float:
        n = len(coords)
        if n < 3:
            return 0.0
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += coords[i][0] * coords[j][1]
            area -= coords[j][0] * coords[i][1]
        avg_lat = sum(c[1] for c in coords) / n
        deg_lon_m = 111320 * math.cos(math.radians(avg_lat))
        area_sq_m = abs(area) / 2 * deg_lon_m * 110574
        return area_sq_m / 10000

    try:
        gtype = geojson_obj.get("type")
        if gtype == "Feature":
            geom = geojson_obj.get("geometry") or {}
        elif gtype == "FeatureCollection":
            return round(sum(estimate_area_ha(f) for f in geojson_obj.get("features", [])), 2)
        else:
            geom = geojson_obj
        gt = geom.get("type") if geom else ""
        coords = geom.get("coordinates", []) if geom else []
        if gt == "Polygon":
            return round(ring_area_ha(coords[0]), 2) if coords else 0.0
        if gt == "MultiPolygon":
            return round(sum(ring_area_ha(poly[0]) for poly in coords), 2)
    except Exception:  # noqa: BLE001, S110 - best-effort estimate, never blocks a save
        pass
    return 0.0


def bbox_and_centroid(geojson_obj: dict) -> tuple[list[float] | None, dict[str, float] | None]:
    """Bounding box `[minLng, minLat, maxLng, maxLat]` and a bbox-center
    centroid `{"lat": .., "lng": ..}` for any Geometry/Feature/
    FeatureCollection. Bbox-center, not a true geometric centroid - matches
    `estimate_area_ha`'s "good enough for display, not geodesically exact"
    precision level (no PostGIS/shapely in this repo).
    """

    def _iter_coords(geom: dict):
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        if gtype == "Point":
            yield coords
        elif gtype in ("LineString", "MultiPoint"):
            yield from coords
        elif gtype in ("Polygon", "MultiLineString"):
            for ring in coords:
                yield from ring
        elif gtype == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    yield from ring
        elif gtype == "GeometryCollection":
            for g in geom.get("geometries", []):
                yield from _iter_coords(g)

    def _geoms(obj: dict):
        gtype = obj.get("type")
        if gtype == "FeatureCollection":
            for f in obj.get("features", []):
                yield from _geoms(f)
        elif gtype == "Feature":
            geom = obj.get("geometry")
            if geom:
                yield geom
        else:
            yield obj

    try:
        lngs: list[float] = []
        lats: list[float] = []
        for geom in _geoms(geojson_obj):
            for pt in _iter_coords(geom):
                lngs.append(float(pt[0]))
                lats.append(float(pt[1]))
        if not lngs:
            return None, None
        bbox = [min(lngs), min(lats), max(lngs), max(lats)]
        centroid = {"lat": (bbox[1] + bbox[3]) / 2, "lng": (bbox[0] + bbox[2]) / 2}
        return bbox, centroid
    except Exception:  # noqa: BLE001 - best-effort, never blocks a save
        return None, None
