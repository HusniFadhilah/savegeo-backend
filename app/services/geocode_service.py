"""Administrative-area geocoding contract shared by the AOI search box and the chatbot.

Pure functions only (no network): ``normalize_candidate`` turns one raw Nominatim
hit into the stable SaveGeo area contract, ``rank_candidates`` orders hits for a
query and marks the single unambiguous one as ``exact_match`` so clients may
auto-select it. Anything not ``exact_match`` must be confirmed by the user.
"""
from __future__ import annotations

import math
import re
from typing import Any

SOURCE = "nominatim/openstreetmap"
MAX_GEOMETRY_VERTICES = 30_000

# Query/name prefixes -> normalized administrative level.
_LEVEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("provinsi", "province"),
    ("kabupaten", "regency"),
    ("kab.", "regency"),
    ("kab", "regency"),
    ("kota", "city"),
    ("kecamatan", "district"),
    ("kec.", "district"),
    ("kec", "district"),
    ("kelurahan", "village"),
    ("desa", "village"),
)
_ADDRESSTYPE_LEVELS = {
    "state": "province",
    "province": "province",
    "region": "province",
    "county": "regency",
    "city": "city",
    "municipality": "city",
    "city_district": "district",
    "suburb": "district",
    "district": "district",
    "village": "village",
    "neighbourhood": "village",
    "quarter": "village",
    "hamlet": "village",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold())


def detect_query_level(query: str) -> tuple[str | None, str]:
    """Return ``(level, bare_name)``; level is None when the query has no level word."""
    text = _clean(query)
    for prefix, level in _LEVEL_PREFIXES:
        if text == prefix:
            continue
        if text.startswith(prefix + " ") or (prefix.endswith(".") and text.startswith(prefix)):
            return level, _clean(text[len(prefix):])
    return None, text


def classify_level(raw: dict[str, Any]) -> str:
    name = str(raw.get("name") or "")
    level, _ = detect_query_level(name)
    if level:
        return level
    return _ADDRESSTYPE_LEVELS.get(str(raw.get("addresstype") or "").casefold(), "other")


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _count_vertices(rings: list) -> int:
    return sum(len(ring) for ring in rings)


def _geometry_status(geometry: Any) -> tuple[dict | None, str]:
    """Validate a Polygon/MultiPolygon. Returns ``(geometry_or_None, status)``."""
    if not isinstance(geometry, dict):
        return None, "missing"
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon") or not isinstance(coords, list) or not coords:
        return None, "unsupported"
    polygons = [coords] if kind == "Polygon" else coords
    vertices = 0
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            return None, "invalid"
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                return None, "invalid"
            for point in ring:
                if not isinstance(point, (list, tuple)) or len(point) < 2:
                    return None, "invalid"
                lon, lat = _finite_float(point[0]), _finite_float(point[1])
                if lon is None or lat is None or abs(lon) > 180 or abs(lat) > 90:
                    return None, "invalid"
            vertices += len(ring)
            if vertices > MAX_GEOMETRY_VERTICES:
                return None, "too_complex"
    return geometry, "ok"


def _bbox(raw: dict[str, Any]) -> list[float] | None:
    bb = raw.get("boundingbox")
    if not isinstance(bb, (list, tuple)) or len(bb) != 4:
        return None
    south, north, west, east = (_finite_float(v) for v in bb)  # Nominatim order
    if None in (south, north, west, east):
        return None
    if not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
        return None
    return [west, south, east, north]


def normalize_candidate(raw: dict[str, Any]) -> dict[str, Any] | None:
    osm_id = raw.get("osm_id")
    osm_type = raw.get("osm_type")
    lat, lng = _finite_float(raw.get("lat")), _finite_float(raw.get("lon"))
    if osm_id is None or not osm_type or lat is None or lng is None:
        return None
    if abs(lat) > 90 or abs(lng) > 180:
        return None

    address = raw.get("address") or {}
    display_name = str(raw.get("display_name") or raw.get("name") or "")
    name = str(raw.get("name") or display_name.split(",")[0]).strip()
    geometry, status = _geometry_status(raw.get("geojson"))
    hierarchy = {
        "country": str(address.get("country") or ""),
        "province": str(address.get("state") or address.get("province") or address.get("region") or ""),
        "regency_or_city": str(
            address.get("city") or address.get("county") or address.get("municipality") or address.get("town") or ""
        ),
        "district": str(address.get("suburb") or address.get("city_district") or ""),
        "village": str(address.get("village") or address.get("neighbourhood") or address.get("quarter") or ""),
    }
    return {
        "area_id": f"osm:{osm_type}:{osm_id}",
        "name": name,
        "display_name": display_name,
        "level": classify_level(raw),
        "hierarchy": hierarchy,
        "centroid": {"lat": lat, "lng": lng},
        "bbox": _bbox(raw),
        "geometry": geometry,
        "geometry_status": status,
        "geometry_ref": f"nominatim:{osm_type}:{osm_id}",
        "source": SOURCE,
        # Backward-compatible fields consumed by AsyncLocationSelect / older chatbot code.
        "osm_id": osm_id,
        "lat": lat,
        "lng": lng,
        "type": str(raw.get("type") or ""),
        "class_": str(raw.get("class") or ""),
        "geojson": geometry,
        "address_info": {
            "province": hierarchy["province"],
            "city": hierarchy["regency_or_city"],
            "district": hierarchy["district"],
            "village": hierarchy["village"],
        },
    }


def _score(candidate: dict[str, Any], query_level: str | None, bare: str) -> int:
    score = 0
    if candidate["class_"] == "boundary":
        score += 40
    if candidate["geometry"] is not None:
        score += 30
    _, name_bare = detect_query_level(candidate["name"])
    if name_bare == bare:
        score += 20
    elif bare and bare in name_bare:
        score += 5
    if query_level and candidate["level"] == query_level:
        score += 25
    return score


def rank_candidates(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Order candidates best-first and mark ``exact_match`` only when unambiguous.

    A candidate is ``exact_match`` iff the query names a level, the candidate is a
    real administrative boundary with usable geometry, its name equals the query
    name and level, and no other candidate matches equally well.
    """
    query_level, bare = detect_query_level(query)
    scored = []
    for candidate in candidates:
        item = dict(candidate)
        item["match_score"] = _score(item, query_level, bare)
        _, name_bare = detect_query_level(item["name"])
        item["_exact"] = bool(
            query_level
            and item["level"] == query_level
            and name_bare == bare
            and item["class_"] == "boundary"
            and item["geometry"] is not None
        )
        scored.append(item)
    scored.sort(key=lambda c: c["match_score"], reverse=True)
    exact = [c for c in scored if c["_exact"]]
    for item in scored:
        item["exact_match"] = len(exact) == 1 and item["_exact"]
        del item["_exact"]
    return scored
