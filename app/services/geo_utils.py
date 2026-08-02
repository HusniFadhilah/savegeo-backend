"""Pure geo helper functions with no DB/GEE dependency."""
from __future__ import annotations

import math
from typing import Any


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
    except Exception:  # noqa: BLE001 - best-effort estimate, never blocks a save
        pass
    return 0.0
