"""Resolve administrative AOIs without resending their vertices through the proxy."""

import re
from collections.abc import Callable

from app.services.gee_common import AnalysisError


def resolve_region_aoi(payload: dict, load_geometry: Callable[[str, str], dict]) -> dict:
    aoi = payload.get("aoi")
    if not isinstance(aoi, dict) or "region" not in aoi:
        return payload
    reference = aoi["region"]
    if not isinstance(reference, dict) or reference.get("level") not in {"province", "city", "district", "village"}:
        raise AnalysisError("Referensi tingkat wilayah AOI tidak valid.", 422)
    codes = reference.get("codes")
    if (
        not isinstance(codes, list) or not 1 <= len(codes) <= 100
        or any(not isinstance(code, str) or not re.fullmatch(r"\d+(?:\.\d+)*", code) or len(code) > 32 for code in codes)
    ):
        raise AnalysisError("Kode wilayah AOI tidak valid.", 422)
    polygons = []

    def append_geometry(item):
        if not isinstance(item, dict):
            raise AnalysisError("Geometri batas wilayah tidak valid.", 502)
        kind = item.get("type")
        if kind == "Feature":
            append_geometry(item.get("geometry"))
        elif kind == "FeatureCollection":
            for feature in item.get("features", []):
                append_geometry(feature)
        elif kind == "GeometryCollection":
            for geometry in item.get("geometries", []):
                append_geometry(geometry)
        elif kind == "Polygon":
            polygons.append(item["coordinates"])
        elif kind == "MultiPolygon":
            polygons.extend(item["coordinates"])
        else:
            raise AnalysisError("Geometri batas wilayah bukan poligon.", 502)

    for code in dict.fromkeys(codes):
        append_geometry(load_geometry(reference["level"], code))
    if not polygons:
        raise AnalysisError("Batas wilayah AOI kosong.", 502)
    geometry = {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else {"type": "MultiPolygon", "coordinates": polygons}
    return {**payload, "aoi": {"geojson": {"type": "Feature", "geometry": geometry, "properties": {}}}}
