from unittest.mock import MagicMock

import pytest

from app.services import landcover_service as service


@pytest.mark.parametrize("operation", [service.analyze_landcover_transition, service.analyze_landcover_change_map])
@pytest.mark.parametrize("area,expected", [(100_000_000, 10), (474_697_220_000, 310)])
def test_both_periods_use_one_bounded_comparison_grid(monkeypatch, operation, area, expected):
    aoi = MagicMock()
    aoi.area.return_value.getInfo.return_value = area
    monkeypatch.setattr(service, "create_geometry_from_payload", lambda _value: aoi)
    monkeypatch.setattr(service, "get_settings", lambda: type("Settings", (), {"max_pixels": 1_000_000_000, "default_landcover_scale": 10})())
    load = MagicMock(return_value=(MagicMock(), {}))
    monkeypatch.setattr(service, "get_landcover_image", load)

    class Compared(Exception):
        pass

    def compare(before, after, before_meta, after_meta, scale, *_args):
        assert scale == expected
        raise Compared()

    monkeypatch.setattr(service, "_comparison_grid", compare)
    with pytest.raises(Compared):
        operation({"aoi": {"geojson": {}}, "dataset": "Dynamic_World", "from_year": 2015, "to_year": 2016,
                   "start_month": 1, "end_month": 12, "dw_probability_threshold": 0.5})
    assert [c.args[1] for c in load.call_args_list] == [2015, 2016]
    assert all(c.kwargs["include_diagnostics"] is False for c in load.call_args_list)
    assert all(c.kwargs["dw_probability_threshold"] == 0.5 for c in load.call_args_list)
