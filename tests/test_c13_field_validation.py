from pathlib import Path

import pytest
from pyproj import Transformer

from scripts.evaluate_c13_field_validation import (
    error_metrics,
    fit_bias_offset,
    load_bu_dessy,
    load_dahana,
    square_plot_bounds,
)


def test_approved_field_fixtures_aggregate_dry_biomass_to_plot_carbon_density():
    dahana = load_dahana(Path("examples/dmrv/pt_dahana_subang_field_inventory.csv"))
    bu_dessy = load_bu_dessy(Path("tests/fixtures/dmrv/bu_dessy_sample.csv"))

    assert len(dahana) == 10
    assert sum(plot["tree_count"] for plot in dahana) == 90
    assert len(bu_dessy) == 1
    assert bu_dessy[0]["tree_count"] == 21
    assert bu_dessy[0]["plot_area_m2"] == 400
    assert bu_dessy[0]["survey_year"] == 2024
    assert bu_dessy[0]["observed_mg_c_ha"] == pytest.approx(111.75093, abs=1e-5)


def test_square_plot_proxy_uses_confirmed_area_and_utm_metric_dimensions():
    bounds = square_plot_bounds(110.4396388889, -7.0527222222, 400)
    forward = Transformer.from_crs("EPSG:4326", "EPSG:32749", always_xy=True)
    west, south = forward.transform(bounds["west"], bounds["south"])
    east, north = forward.transform(bounds["east"], bounds["north"])

    assert east - west == pytest.approx(20, abs=0.01)
    assert north - south == pytest.approx(20, abs=0.01)


def test_bias_offset_uses_only_plots_with_model_predictions():
    calibration = [
        {"observed_mg_c_ha": 15.0, "prediction_mg_c_ha": 10.0},
        {"observed_mg_c_ha": 21.0, "prediction_mg_c_ha": 20.0},
        {"observed_mg_c_ha": 31.0, "prediction_mg_c_ha": 30.0},
        {"observed_mg_c_ha": 90.0, "prediction_mg_c_ha": None},
    ]

    assert fit_bias_offset(calibration) == pytest.approx(7 / 3)
    with pytest.raises(ValueError, match="At least three"):
        fit_bias_offset(calibration[:2])


def test_single_independent_plot_metrics_are_labeled_preliminary():
    result = error_metrics([111.75], [120.0])

    assert result["n_plots"] == 1
    assert result["bias_mg_c_ha"] == pytest.approx(8.25)
    assert result["rmse_mg_c_ha"] == pytest.approx(8.25)
    assert result["mae_mg_c_ha"] == pytest.approx(8.25)
    assert result["r2"] is None
    assert result["qualification"] == "single_plot_preliminary_not_generalizable"
