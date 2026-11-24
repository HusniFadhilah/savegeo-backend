from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from pyproj import Transformer

from scripts.evaluate_c13_field_validation import (
    error_metrics,
    failure_qualification,
    fit_bias_offset,
    load_bu_dessy,
    load_dahana,
    _predict_plot_means,
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


def test_c13_unavailable_provider_is_not_mislabeled_as_missing_model():
    transport_error = type("TransportError", (Exception,), {})("offline")

    assert failure_qualification(transport_error) == "not_computed_provider_transport_failure"
    assert failure_qualification(ValueError("invalid model")) == "not_computed_model_or_inference_failure"


def test_field_predictions_are_requested_per_plot_not_one_large_bounding_box(monkeypatch):
    calls = []

    class Geometry:
        @staticmethod
        def Polygon(coordinates, **_kwargs):
            return SimpleNamespace(coordinates=coordinates, bounds=lambda **kwargs: ("bounds", coordinates))

    class FeatureCollection:
        def __init__(self, features):
            self.features = features

    class Reducer:
        @staticmethod
        def mean():
            return SimpleNamespace(combine=lambda *_args, **_kwargs: "mean+count")

        @staticmethod
        def count():
            return "count"

    class FakeImage:
        def reduceRegions(self, *, collection, **_kwargs):
            assert len(collection.features) == 1
            feature = collection.features[0]
            self.plot_id = feature[1]["plot_id"]
            return self

        def getInfo(self):
            return {"features": [{"properties": {
                "plot_id": self.plot_id,
                "carbon_estimated_mean": 25.5,
                "carbon_estimated_count": 1,
            }}]}

    ee = SimpleNamespace(
        Geometry=Geometry,
        Feature=lambda geometry, properties: (geometry, properties),
        FeatureCollection=FeatureCollection,
        Reducer=Reducer,
    )
    monkeypatch.setitem(sys.modules, "ee", ee)

    class Engine:
        last_s2_image_count = None

        def predict_for_region(self, **kwargs):
            calls.append(kwargs["roi"])
            self.last_s2_image_count = 3
            return FakeImage()

    predictions, image_count, pixel_counts = _predict_plot_means(
        Engine(),
        [
            {"plot_id": "near", "longitude": 110.4, "latitude": -7.0, "plot_area_m2": 400},
            {"plot_id": "far", "longitude": 120.4, "latitude": -3.0, "plot_area_m2": 400},
        ],
        2024,
    )

    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert predictions == {"near": 25.5, "far": 25.5}
    assert image_count == 3
    assert pixel_counts == {"near": 1, "far": 1}
