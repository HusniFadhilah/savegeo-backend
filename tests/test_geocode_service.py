from __future__ import annotations

import pytest

from app.services import geocode_service as gs


def _square(lon: float = 110.0, lat: float = -7.0, size: float = 0.1) -> dict:
    ring = [
        [lon, lat],
        [lon + size, lat],
        [lon + size, lat + size],
        [lon, lat + size],
        [lon, lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _raw(name: str, osm_id: int, *, addresstype: str = "city", cls: str = "boundary", **extra) -> dict:
    base = {
        "osm_type": "relation",
        "osm_id": osm_id,
        "lat": "-7.0",
        "lon": "110.4",
        "name": name,
        "display_name": f"{name}, Jawa Tengah, Indonesia",
        "class": cls,
        "type": "administrative",
        "addresstype": addresstype,
        "boundingbox": ["-7.1", "-6.9", "110.3", "110.5"],
        "address": {"state": "Jawa Tengah", "country_code": "id"},
        "geojson": _square(),
    }
    base.update(extra)
    return base


@pytest.mark.parametrize(
    ("query", "level", "bare"),
    [
        ("Kota Semarang", "city", "semarang"),
        ("kabupaten semarang", "regency", "semarang"),
        ("Kab. Semarang", "regency", "semarang"),
        ("Kecamatan Tembalang", "district", "tembalang"),
        ("Provinsi Jawa Tengah", "province", "jawa tengah"),
        ("Semarang", None, "semarang"),
    ],
)
def test_detect_query_level(query, level, bare):
    assert gs.detect_query_level(query) == (level, bare)


def test_classify_level_prefers_name_prefix_over_addresstype():
    assert gs.classify_level(_raw("Kabupaten Semarang", 1, addresstype="county")) == "regency"
    assert gs.classify_level(_raw("Kota Semarang", 2, addresstype="city")) == "city"
    assert gs.classify_level(_raw("Tembalang", 3, addresstype="suburb")) == "district"
    assert gs.classify_level(_raw("Jawa Tengah", 4, addresstype="state")) == "province"
    assert gs.classify_level(_raw("Somewhere", 5, addresstype="unknown")) == "other"


def test_normalize_candidate_contract():
    candidate = gs.normalize_candidate(_raw("Kota Semarang", 1234))
    assert candidate is not None
    assert candidate["area_id"] == "osm:relation:1234"
    assert candidate["name"] == "Kota Semarang"
    assert candidate["level"] == "city"
    assert candidate["centroid"] == {"lat": -7.0, "lng": 110.4}
    assert candidate["bbox"] == [110.3, -7.1, 110.5, -6.9]
    assert candidate["geometry"]["type"] == "Polygon"
    assert candidate["geometry_ref"] == "nominatim:relation:1234"
    assert candidate["source"] == "nominatim/openstreetmap"
    assert candidate["hierarchy"]["province"] == "Jawa Tengah"
    # Backward-compatible keys used by AsyncLocationSelect / chatbot.
    assert candidate["osm_id"] == 1234
    assert candidate["geojson"] is candidate["geometry"]
    assert candidate["display_name"].startswith("Kota Semarang")


def test_normalize_candidate_drops_non_polygon_geometry_but_keeps_candidate():
    raw = _raw("Kota Semarang", 9, geojson={"type": "Point", "coordinates": [110, -7]})
    candidate = gs.normalize_candidate(raw)
    assert candidate is not None
    assert candidate["geometry"] is None
    assert candidate["geojson"] is None


def test_normalize_candidate_rejects_missing_identity_or_coordinates():
    assert gs.normalize_candidate({"lat": "1", "lon": "2"}) is None
    assert gs.normalize_candidate(_raw("X", 1, lat="nan")) is None
    assert gs.normalize_candidate(_raw("X", 1, lat="95")) is None


def test_normalize_candidate_caps_geometry_complexity():
    ring = [[110 + i * 1e-5, -7 + (i % 2) * 1e-5] for i in range(gs.MAX_GEOMETRY_VERTICES + 50)]
    ring.append(ring[0])
    raw = _raw("Kota Semarang", 77, geojson={"type": "Polygon", "coordinates": [ring]})
    candidate = gs.normalize_candidate(raw)
    assert candidate is not None
    assert candidate["geometry"] is None
    assert candidate["geometry_status"] == "too_complex"


def test_rank_prefers_city_over_regency_for_kota_query():
    kab = gs.normalize_candidate(_raw("Kabupaten Semarang", 1, addresstype="county"))
    kota = gs.normalize_candidate(_raw("Kota Semarang", 2, addresstype="city"))
    ranked = gs.rank_candidates([kab, kota], "Kota Semarang")
    assert [c["area_id"] for c in ranked] == ["osm:relation:2", "osm:relation:1"]
    assert ranked[0]["exact_match"] is True
    assert ranked[1]["exact_match"] is False


def test_rank_prefers_regency_for_kabupaten_query():
    kab = gs.normalize_candidate(_raw("Kabupaten Semarang", 1, addresstype="county"))
    kota = gs.normalize_candidate(_raw("Kota Semarang", 2, addresstype="city"))
    ranked = gs.rank_candidates([kota, kab], "Kabupaten Semarang")
    assert ranked[0]["area_id"] == "osm:relation:1"
    assert ranked[0]["exact_match"] is True


def test_rank_without_level_word_is_ambiguous():
    kab = gs.normalize_candidate(_raw("Kabupaten Semarang", 1, addresstype="county"))
    kota = gs.normalize_candidate(_raw("Kota Semarang", 2, addresstype="city"))
    ranked = gs.rank_candidates([kab, kota], "Semarang")
    assert not any(c["exact_match"] for c in ranked)


def test_rank_demotes_non_boundary_and_geometryless_results():
    poi = gs.normalize_candidate(_raw("Kota Semarang Cafe", 3, cls="amenity", addresstype="amenity"))
    kota = gs.normalize_candidate(_raw("Kota Semarang", 2))
    no_geom = gs.normalize_candidate(_raw("Kota Semarang", 4, geojson=None))
    ranked = gs.rank_candidates([poi, no_geom, kota], "Kota Semarang")
    assert ranked[0]["area_id"] == "osm:relation:2"


def test_rank_two_equally_exact_matches_are_not_auto_selectable():
    a = gs.normalize_candidate(_raw("Kota Semarang", 2))
    b = gs.normalize_candidate(_raw("Kota Semarang", 5))
    ranked = gs.rank_candidates([a, b], "Kota Semarang")
    assert all(c["exact_match"] is False for c in ranked)


def test_geocode_search_route_returns_ranked_contract(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import utils
    from app.core.security import get_current_app_viewer

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                _raw("Kabupaten Semarang", 1, addresstype="county"),
                _raw("Kota Semarang", 2),
            ]

    captured = {}

    def fake_get(url, params=None, **kwargs):
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(utils.requests, "get", fake_get)
    app = FastAPI()
    app.include_router(utils.router)
    app.dependency_overrides[get_current_app_viewer] = lambda: object()
    response = TestClient(app).get("/utils/geocode/search", params={"q": "Kota Semarang"})

    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["area_id"] for r in results] == ["osm:relation:2", "osm:relation:1"]
    assert results[0]["exact_match"] is True
    assert captured["params"]["polygon_threshold"] > 0


def test_geocode_search_route_rejects_non_list_upstream(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import utils
    from app.core.security import get_current_app_viewer

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": "rate limited"}

    monkeypatch.setattr(utils.requests, "get", lambda *a, **k: FakeResponse())
    app = FastAPI()
    app.include_router(utils.router)
    app.dependency_overrides[get_current_app_viewer] = lambda: object()
    response = TestClient(app).get("/utils/geocode/search", params={"q": "Semarang"})
    assert response.status_code == 502
