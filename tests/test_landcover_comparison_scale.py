from unittest.mock import MagicMock

import pytest

from app.services import landcover_service as service
from app.services.gee_common import AnalysisError


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


@pytest.mark.parametrize("force", [True, False])
def test_statistics_force_the_grid_but_map_tiles_use_a_flexible_pyramid(monkeypatch, force):
    before, after = MagicMock(), MagicMock()
    for image in (before, after):
        image.toInt32.return_value = image
        image.reproject.return_value = image
        image.setDefaultProjection.return_value = image
        image.updateMask.return_value = image
    ee = MagicMock()
    monkeypatch.setattr(service, "ee", ee)
    service._comparison_grid(before, after, {"year": 2015, "dataset": "Dynamic_World"},
                             {"year": 2026, "dataset": "Dynamic_World"}, 310, force_grid=force)
    for image in (before, after):
        if force:
            image.reproject.assert_called_once()
            image.setDefaultProjection.assert_not_called()
        else:
            image.reproject.assert_not_called()
            image.setDefaultProjection.assert_called_once()


def test_change_map_missing_years_returns_controlled_client_error():
    with pytest.raises(AnalysisError) as error:
        service.analyze_landcover_change_map({"aoi": {"geojson": {}}, "dataset": "Dynamic_World"})

    assert error.value.status_code == 400
    assert "from_year dan to_year" in str(error.value)
