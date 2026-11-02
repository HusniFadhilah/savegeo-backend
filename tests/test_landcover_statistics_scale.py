import pytest

from app.services.gee_common import AnalysisError
from app.services.landcover_service import landcover_statistics_scale


def test_small_aoi_keeps_native_resolution():
    assert landcover_statistics_scale(2_830.78 * 1_000_000, 10, 1_000_000_000) == 10


def test_sumatra_statistics_fit_a_bounded_pixel_budget():
    area = 474_697.22 * 1_000_000
    scale = landcover_statistics_scale(area, 10, 1_000_000_000)
    assert scale > 10
    assert area / scale**2 <= 5_000_000
    assert scale % 10 == 0


def test_an_explicit_coarser_scale_is_preserved():
    assert landcover_statistics_scale(474_697.22 * 1_000_000, 1000, 1_000_000_000) == 1000


def test_smaller_server_pixel_limit_is_respected():
    area = 100 * 1_000_000
    scale = landcover_statistics_scale(area, 10, 100_000)
    assert area / scale**2 <= 100_000


@pytest.mark.parametrize("area,scale,pixels", [(0,10,1000),(float('nan'),10,1000),(1,0,1000),(1,10,0)])
def test_invalid_geometry_or_limits_fail_explicitly(area,scale,pixels):
    with pytest.raises(AnalysisError):
        landcover_statistics_scale(area,scale,pixels)
