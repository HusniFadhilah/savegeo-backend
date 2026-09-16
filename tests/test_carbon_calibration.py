from __future__ import annotations

import zipfile

import pytest

from app.services.carbon_calibration_service import (
    build_plot_polygon,
    lopo_linear_calibration,
    plot_carbon_density,
    safe_extract_zip,
    tree_carbon_from_dbh,
)


def test_dahana_allometry_and_carbon_fraction_are_reproducible():
    result = tree_carbon_from_dbh(10)
    assert result["agb_kg"] == pytest.approx(3.42 * 10**1.15)
    assert result["bgb_kg"] == pytest.approx(0.207 * 10**1.668)
    assert result["carbon_agb_kg"] == pytest.approx(result["agb_kg"] * 0.47)
    assert result["carbon_bgb_kg"] == pytest.approx(result["bgb_kg"] * 0.47)


def test_plot_density_converts_kg_plot_to_mg_c_per_ha():
    assert plot_carbon_density(400) == pytest.approx(10)


def test_plot_polygon_preserves_original_coordinates_and_area():
    result = build_plot_polygon("T1", [(0, 0), (20, 0), (20, 20), (0, 20)])
    assert result["crs"] == "EPSG:32748"
    assert result["area_m2"] == pytest.approx(400)
    assert result["area_ok"] is True
    assert result["original"] == result["normalized"]
    assert len(result["original"]["geometry"]["coordinates"][0]) == 5


def test_plot_polygon_rejects_self_intersection():
    with pytest.raises(ValueError, match="self-intersects"):
        build_plot_polygon("T1", [(0, 0), (20, 20), (20, 0), (0, 20)])


def test_lopo_uses_one_held_out_plot_each_time():
    rows = [{"plot_id": f"T{i}", "predicted": float(i), "observed": float(i * 2)} for i in range(1, 6)]
    result = lopo_linear_calibration(rows)
    assert result["status"] == "computed"
    assert result["validation_method"] == "leave_one_plot_out"
    assert len(result["rows"]) == 5
    assert {row["plot_id"] for row in result["rows"]} == {f"T{i}" for i in range(1, 6)}


def test_zip_slip_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../../outside.txt", "blocked")
    with pytest.raises(ValueError, match="ZIP Slip"):
        safe_extract_zip(archive, tmp_path / "out")
