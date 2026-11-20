import pytest

from app.services.analysis_preflight import preflight


def test_bounding_box_is_checked_as_geometry():
    result = preflight("carbon", {"aoi": {"west": 110.35, "south": -7.08, "east": 110.42, "north": -7.01}})
    assert result["status"] == "ready"
    assert result["checks"]["geometry"]["area_ha"] > 0


@pytest.mark.parametrize("region", [
    {"scope": "indonesia"},
    {"scope": "island", "code": "Kalimantan"},
    {"level": "village", "codes": ["33.74.01.1001"]},
])
def test_region_reference_is_deferred_for_server_resolution(region):
    result = preflight("carbon", {"aoi": {"region": region}})
    assert result["status"] == "ready"
    assert result["checks"]["geometry"]["status"] == "deferred"


@pytest.mark.parametrize("aoi", [
    {"west": 111, "south": -7, "east": 110, "north": -6},
    {"region": "invalid"},
])
def test_invalid_aoi_is_blocked(aoi):
    assert preflight("carbon", {"aoi": aoi})["status"] == "blocked"
