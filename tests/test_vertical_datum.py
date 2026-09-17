import pytest

from app.services.vertical_datum_service import (
    EGM2008_VERTICAL_CRS,
    convert_ellipsoidal_to_orthometric,
    egm2008_status,
)


def test_egm2008_scalar_formula_preserves_source_and_sign():
    result = convert_ellipsoidal_to_orthometric(
        242.83, 27.41, source_vertical_reference="WGS84_ELLIPSOID"
    )
    assert result["orthometric_height_m"] == pytest.approx(215.42)
    assert result["transformation_method"] == "H = h - N"
    assert result["vertical_crs"] == EGM2008_VERTICAL_CRS
    assert result["vertical_reference_status"] == "converted"
    assert result["source_height_m"] == 242.83


@pytest.mark.parametrize("source", ["", "unknown", "EGM96"])
def test_unknown_or_wrong_vertical_reference_is_not_assumed(source):
    with pytest.raises(ValueError):
        convert_ellipsoidal_to_orthometric(100, 20, source_vertical_reference=source)


def test_missing_grid_is_reported_without_claiming_availability(monkeypatch):
    monkeypatch.delenv("SAVEGEO_EGM2008_GRID", raising=False)
    status = egm2008_status()
    assert status["model"] == "EGM2008"
    assert status["available"] is False
    assert status["checksum"] is None
