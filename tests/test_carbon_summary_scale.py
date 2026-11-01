import math

import pytest

from app.services.carbon_pool_service import summary_scale


@pytest.mark.parametrize("native,area,budget", [(250, 8e12, 1e8), (300, 1e12, 1e8), (250, 8e12, 100_000), (300, 1e8, 1e8)])
def test_summary_scale_respects_native_resolution_and_pixel_budget(native, area, budget):
    scale = summary_scale(native, area, budget)
    assert scale >= native
    assert math.ceil(area / scale**2) <= min(budget, 1_000_000)
