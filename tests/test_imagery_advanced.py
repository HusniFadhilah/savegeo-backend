import pytest

from app.services.advanced_imagery_service import (
    thermal_calibration,
    validate_insar_pair,
)
from app.services.gee_common import AnalysisError


def test_thermal_calibration_requires_explicit_product_metadata():
    result = thermal_calibration({
        "values": [0, 1000],
        "scale": 0.01,
        "offset": 273.15,
        "source_unit": "K",
        "output_unit": "C",
    })
    assert result["values"] == pytest.approx([-0.0, 10.0])
    assert result["output_unit"] == "C"
    assert result["calibrated"] is True


def test_thermal_calibration_does_not_guess_scale_offset():
    with pytest.raises(AnalysisError, match="scale dan offset"):
        thermal_calibration({"values": [1, 2], "source_unit": "K"})


def test_insar_pair_requires_two_slc_products():
    result = validate_insar_pair({
        "master": {"id": "master", "product_type": "SLC"},
        "slave": {"id": "slave", "product_type": "SLC"},
    })
    assert result["master_id"] == "master"
    assert result["slave_id"] == "slave"
    assert result["ready"] in {True, False}

    with pytest.raises(AnalysisError, match="SLC"):
        validate_insar_pair({
            "master": {"id": "a", "product_type": "GRD"},
            "slave": {"id": "b", "product_type": "SLC"},
        })
