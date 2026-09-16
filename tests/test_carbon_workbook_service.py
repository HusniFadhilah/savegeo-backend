from __future__ import annotations

from openpyxl import Workbook

from app.services.carbon_workbook_service import (
    normalize_species,
    parse_dms,
    parse_workbook,
    safe_evaluate_formula,
    validate_raster_coverage,
)


def test_dms_and_species_normalization_preserve_source_semantics():
    coordinate = parse_dms("7� 1'47.68\"S - 109�35'49.99\"E")
    assert coordinate["status"] == "valid"
    assert coordinate["latitude"] < 0
    assert coordinate["longitude"] > 0
    assert normalize_species("swietenia mahagoni / Mahoni")["scientific_name"] == "Swietenia mahagoni"


def test_formula_evaluator_recalculates_only_whitelisted_algebra():
    assert safe_evaluate_formula("=C6/PI()", {"C6": 31.4}) is not None
    assert safe_evaluate_formula("=0.027*(D6^2.23)", {"D6": "=C6/PI()", "C6": 31.4}) is not None
    assert safe_evaluate_formula("=SUM(C6:C7)", {"C6": 1, "C7": 2}) is None
    assert safe_evaluate_formula("=A1+#REF!", {"A1": 1}) is None


def test_workbook_import_excludes_broken_sheet_and_keeps_provenance(tmp_path):
    workbook_path = tmp_path / "survey.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "KHDTK"
    sheet.append(["Biomassa Atas Permukaan"])
    sheet.append(["No. Transek : T1 (KHDTK)"])
    sheet.append(["Koordinat: 7� 1'47.68\"S - 109�35'49.99\"E"])
    sheet.append(["No.", "Jenis Pohon", "Keliling (cm)", "Diameter (cm)", "Tinggi(cm)", "Biomassa (kg)"])
    sheet.append([1, "Albizia chinensis / Sengon", 31.4, "=C5/PI()", 1200, "=0.027*(D5^2.23)"])
    broken = workbook.create_sheet("Sheet3")
    broken.append(["Total", "=SUM(#REF!)"])
    workbook.save(workbook_path)

    result = parse_workbook(workbook_path, "test", 2026)
    assert len(result["records"]) == 1
    assert result["records"][0]["carbon_pool"] == "agb_carbon"
    assert result["records"][0]["provenance"]["sheet"] == "KHDTK"
    assert "Sheet3" in result["audit"]["training_excluded_sheets"]
    assert result["records"][0]["agb_carbon_kg"] is not None
    assert result["records"][0]["plot_area_m2"] is None


def test_dem_coverage_blocks_outside_plot_without_shifting_coordinates():
    result = validate_raster_coverage(
        [{"plot_id": "KHDTK:T1", "latitude": -7.0, "longitude": 110.0, "coordinate_status": "valid"}],
        (110.43, -7.11, 110.44, -7.10),
        "EPSG:4326",
    )
    assert result["blocking"] is True
    assert result["plots"][0]["latitude"] == -7.0
    assert result["plots"][0]["longitude"] == 110.0
