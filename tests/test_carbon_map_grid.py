from unittest.mock import MagicMock

import pytest

from app.services.carbon_map_grid import WEB_MAP_CRS, apply_carbon_map_grid
from app.services.carbon_pool_service import _carbon_value_kind, _reference_support_scale
from app.services.carbon_service import _carbon_dataset_value_kind
from app.services.config_service import validate_carbon_pool_map_resolution_updates


def test_map_grid_is_applied_in_web_map_crs_without_changing_analysis_code_path():
    image = MagicMock()
    projected = MagicMock()
    image.reproject.return_value = projected

    result = apply_carbon_map_grid(image, 60)

    assert result is projected
    image.reproject.assert_called_once_with(crs=WEB_MAP_CRS, scale=60)


@pytest.mark.parametrize("resolution", [None, 0, -1, True, "30", 30.5, float("nan"), float("inf")])
def test_invalid_map_grid_is_not_applied(resolution):
    image = MagicMock()

    assert apply_carbon_map_grid(image, resolution) is image
    image.reproject.assert_not_called()


def test_model_output_is_not_labeled_as_a_field_measurement_or_imputation():
    meta = {"data_value_kind": "measured", "resolution": 250}

    assert _carbon_value_kind("FIELD_REFERENCE", meta, {"name": "model"}, "random_forest", {}) == "modeled"
    assert _carbon_dataset_value_kind({"key": "GEDI_L4D"}) == "imputed"
    assert _carbon_dataset_value_kind({"key": "FIELD_REFERENCE", "data_value_kind": "measured"}) == "measured"


def test_derived_pool_methods_keep_their_qualification():
    assert _carbon_value_kind("SOIL", {}, {"name": "model"}, "soil_stock_integration", {}) == "derived"
    assert _carbon_value_kind(
        "LITTER", {}, {"name": "model"}, "saved_reference_ensemble",
        {"source_data_kind": "proxy_not_field_measurement"},
    ) == "derived"


def test_reference_support_uses_training_scale_when_present_and_missing_stays_missing():
    assert _reference_support_scale(
        {"resolution": 250}, {"training_sample_scale_m": 1000}, {"name": "model"}
    ) == 1000
    assert _reference_support_scale({}, {}, None) is None


def test_admin_map_grid_configuration_accepts_only_supported_values():
    assert validate_carbon_pool_map_resolution_updates({
        "carbon.pool.agb.map_resolution_m": "reference",
        "carbon.pool.soc.map_resolution_m": 250,
    }) == {}
    errors = validate_carbon_pool_map_resolution_updates({
        "carbon.pool.agb.map_resolution_m": "15",
        "carbon.pool.soc.map_resolution_m": "30.5",
    })
    assert set(errors) == {"carbon.pool.agb.map_resolution_m", "carbon.pool.soc.map_resolution_m"}
