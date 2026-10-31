import pytest

from app.services.aoi_reference import resolve_region_aoi
from app.services.gee_common import AnalysisError


def test_reference_keeps_exact_boundary_and_holes():
    polygon = {"type": "Polygon", "coordinates": [
        [[110, -7], [111, -7], [111, -6], [110, -7]],
        [[110.1, -6.9], [110.2, -6.9], [110.2, -6.8], [110.1, -6.9]],
    ]}
    calls = []

    def provider(level, code):
        calls.append((level, code))
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": polygon}]}

    original = {"aoi": {"region": {"level": "city", "codes": ["3374"]}}, "year": 2026}
    resolved = resolve_region_aoi(original, provider)
    assert resolved["aoi"]["geojson"]["geometry"] == polygon
    assert resolved["year"] == 2026
    assert "region" in original["aoi"]
    assert calls == [("city", "3374")]


def test_multi_region_combines_polygons_without_duplicating_codes():
    polygon = {"type": "Polygon", "coordinates": [[[110, -7], [111, -7], [111, -6], [110, -7]]]}
    calls = []

    def provider(level, code):
        calls.append(code)
        return polygon

    resolved = resolve_region_aoi({"aoi": {"region": {"level": "city", "codes": ["3374", "3374", "3375"]}}}, provider)
    assert resolved["aoi"]["geojson"]["geometry"] == {"type": "MultiPolygon", "coordinates": [polygon["coordinates"], polygon["coordinates"]]}
    assert calls == ["3374", "3375"]


@pytest.mark.parametrize("reference", [{"level": "http://other", "codes": ["3374"]}, {"level": "city", "codes": []}, {"level": "city", "codes": ["../../etc"]}, {"level": "city", "codes": "3374"}])
def test_invalid_reference_never_contacts_provider(reference):
    def provider(*_):
        pytest.fail("invalid reference reached provider")

    with pytest.raises(AnalysisError) as caught:
        resolve_region_aoi({"aoi": {"region": reference}}, provider)
    assert caught.value.status_code == 422


def test_geojson_payload_is_unchanged():
    payload = {"aoi": {"geojson": {"type": "Polygon", "coordinates": []}}}
    assert resolve_region_aoi(payload, lambda *_: pytest.fail("unexpected provider call")) is payload
