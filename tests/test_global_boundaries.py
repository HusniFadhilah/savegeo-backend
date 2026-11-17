from fastapi import HTTPException
from shapely.geometry import box, mapping

from app.api.routes import global_boundaries


def _feature(name, bounds):
    return {
        "type": "Feature",
        "properties": {"shapeName": name},
        "geometry": mapping(box(*bounds)),
    }


def test_adm3_and_adm4_options_are_filtered_by_the_immediate_parent(monkeypatch):
    layers = {
        "ADM2": [_feature("Parent A", (0, 0, 10, 10)), _feature("Parent B", (20, 20, 30, 30))],
        "ADM3": [
            _feature("Child in A", (1, 1, 4, 4)),
            _feature("Child in B", (21, 21, 24, 24)),
            _feature("Outside", (40, 40, 45, 45)),
        ],
        "ADM4": [
            _feature("Leaf in A", (2, 2, 3, 3)),
            _feature("Leaf in B", (22, 22, 23, 23)),
            _feature("Outside", (50, 50, 51, 51)),
        ],
    }

    def fake_geometry(_iso3, level):
        return {"boundaryType": level, "boundarySource": "test"}, layers[level]

    monkeypatch.setattr(global_boundaries, "_geometry_features", fake_geometry)

    adm3 = global_boundaries.list_global_boundaries("IDN", "ADM3", parent_index=0)
    adm4 = global_boundaries.list_global_boundaries("IDN", "ADM4", parent_index=0)

    assert [row["name"] for row in adm3] == ["Child in A"]
    assert [row["name"] for row in adm4] == ["Leaf in A"]
    assert adm3[0]["level"] == "ADM3"
    assert adm4[0]["level"] == "ADM4"


def test_adm3_and_adm4_require_the_parent_index():
    for level in ("ADM3", "ADM4"):
        try:
            global_boundaries.list_global_boundaries("IDN", level, parent_index=None)
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "parent_index is required" in exc.detail
        else:
            raise AssertionError(f"{level} unexpectedly accepted a missing parent_index")


def test_geo_boundaries_download_urls_are_restricted_and_github_raw_is_canonicalized():
    assert global_boundaries._trusted_download_url(
        "https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/IDN/ADM1/data.geojson"
    ) == "https://raw.githubusercontent.com/wmgeolab/geoBoundaries/main/releaseData/IDN/ADM1/data.geojson"
    assert global_boundaries._trusted_download_url(
        "https://www.geoboundaries.org/api/current/gbOpen/IDN/ADM1/"
    ).startswith("https://www.geoboundaries.org/")
    for url in (
        "http://www.geoboundaries.org/api/current/gbOpen/IDN/ADM1/",
        "https://127.0.0.1/api/current/gbOpen/IDN/ADM1/",
        "https://github.com/attacker/repo/raw/main/data.geojson",
        "https://github.com/wmgeolab/geoBoundaries/archive/main.zip",
    ):
        try:
            global_boundaries._trusted_download_url(url)
        except global_boundaries.BoundaryUnavailable:
            pass
        else:
            raise AssertionError(f"unsupported geoBoundaries URL accepted: {url}")


def test_geo_boundaries_download_uses_shared_bounded_transport(monkeypatch):
    calls = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"type": "FeatureCollection", "features": []}

    def fake_get(url, **kwargs):
        calls.update(url=url, kwargs=kwargs)
        return Response()

    monkeypatch.setattr(global_boundaries.requests, "get", fake_get)
    result = global_boundaries._download_json(
        "https://github.com/wmgeolab/geoBoundaries/raw/main/data.geojson",
        max_bytes=1024,
    )
    assert result["type"] == "FeatureCollection"
    assert calls["url"] == "https://raw.githubusercontent.com/wmgeolab/geoBoundaries/main/data.geojson"
    assert calls["kwargs"]["max_bytes"] == 1024
    assert calls["kwargs"]["timeout"] == 60
