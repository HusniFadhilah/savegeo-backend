"""OSM company-boundary import splits Overpass queries by region (no network)."""
from __future__ import annotations

import re

from app.api.routes import admin

_BBOX = re.compile(r"\[bbox:([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\]")


def _bbox(query: str) -> tuple[float, float, float, float]:
    match = _BBOX.search(query)
    assert match, query
    return tuple(float(value) for value in match.groups())  # type: ignore[return-value]


def test_every_query_is_bounded_and_keeps_indonesia_area_filter(monkeypatch):
    queries = []
    monkeypatch.setattr(admin, "_fetch_osm_overpass", lambda q: queries.append(q) or ({"elements": []}, ""))
    elements, failed, warnings = admin._collect_osm_elements(["energy"], lambda _message: None)
    assert (elements, failed, warnings) == ([], [], [])
    assert len(queries) == len(admin._OSM_INDONESIA_REGIONS)
    for query in queries:
        south, west, north, east = _bbox(query)
        assert south < north and west < east
        assert 'area["ISO3166-1"="ID"]' in query and "(area.searchArea)" in query


def test_timed_out_regions_are_split_and_results_deduplicated(monkeypatch):
    calls = []

    def fake_fetch(query):
        south, west, north, east = _bbox(query)
        calls.append((south, west, north, east))
        if (north - south) * (east - west) > 40:  # whole island boxes "time out"
            return None, "HTTP 504 dari https://overpass.example"
        shared = {"type": "way", "id": 1, "tags": {"name": "Tambang A"}}
        own = {"type": "way", "id": len(calls) + 100, "tags": {"name": f"Lokasi {len(calls)}"}}
        return {"elements": [shared, own]}, ""

    monkeypatch.setattr(admin, "_fetch_osm_overpass", fake_fetch)
    messages = []
    elements, failed, warnings = admin._collect_osm_elements(["mining"], messages.append)

    assert failed == []
    assert warnings == []
    assert len(calls) > len(admin._OSM_INDONESIA_REGIONS)  # large regions were subdivided
    ids = [element["id"] for element in elements]
    assert ids.count(1) == 1  # duplicates across overlapping boxes collapse
    assert any("bagian" in message for message in messages)


def test_partial_region_failure_is_a_warning_not_a_failed_import(monkeypatch):
    papua = dict(admin._OSM_INDONESIA_REGIONS)["Papua"]

    def fake_fetch(query):
        south, west, north, east = _bbox(query)
        if papua[0] <= south and north <= papua[2] and papua[1] <= west and east <= papua[3]:
            return None, "HTTP 504"
        return {"elements": [{"type": "way", "id": int(abs(south * 1000) + abs(west)), "tags": {"name": "x"}}]}, ""

    monkeypatch.setattr(admin, "_fetch_osm_overpass", fake_fetch)
    elements, failed, warnings = admin._collect_osm_elements(["forestry"], lambda _message: None)
    assert elements
    assert failed == []
    assert warnings == ["forestry: sebagian wilayah gagal diambil (Papua)"]


def test_industry_fails_only_when_every_region_fails(monkeypatch):
    monkeypatch.setattr(admin, "_fetch_osm_overpass", lambda q: (None, "HTTP 504 dari mirror"))
    monkeypatch.setattr(admin, "_OSM_MAX_SPLIT_DEPTH", 0)
    elements, failed, _warnings = admin._collect_osm_elements(["plantation"], lambda _message: None)
    assert elements == []
    assert failed == ["plantation: HTTP 504 dari mirror"]
