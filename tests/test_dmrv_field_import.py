import datetime as dt
import io

import pytest
from openpyxl import Workbook

from app.services.dmrv.field_import import (
    FieldImportError,
    ImportOptions,
    read_table,
    suggest_mapping,
    validate_rows,
)

BU_HEADERS = ["Point ID", "Jenis Pohon", "Keliling (cm)", "Diameter (cm)", "Tinggi(cm)", "Biomassa (kg)", "Transek", "Sheet"]
BASE = {"plot_code": "1", "survey_year": "2024", "tree_code": "1", "circumference_cm": "70", "dbh_cm": "22.28"}


def run(*overrides, options=None, drop=()):
    rows = []
    for override in overrides:
        row = {**BASE, **override}
        for key in drop:
            row.pop(key, None)
        rows.append(row)
    fields = sorted({key for row in rows for key in row})
    return validate_rows(rows, {field: field for field in fields}, options or ImportOptions(height_unit="m"))


def codes(report, level=None):
    return [(issue.row, issue.code) for issue in report.issues if level is None or issue.level == level]


def test_suggest_mapping_recognises_bu_dessy_headers():
    assert suggest_mapping(BU_HEADERS) == {
        "plot_code": "Point ID",
        "species": "Jenis Pohon",
        "circumference_cm": "Keliling (cm)",
        "dbh_cm": "Diameter (cm)",
        "height": "Tinggi(cm)",
        "biomass_kg": "Biomassa (kg)",
    }


def test_suggest_mapping_never_uses_one_header_twice():
    mapping = suggest_mapping(["Tinggi (cm)", "Tinggi (m)"])
    assert list(mapping.values()).count("Tinggi (cm)") + list(mapping.values()).count("Tinggi (m)") == 1


def test_read_table_handles_bom_semicolon_and_comma_decimals():
    data = "﻿plot_code;tree_code;dbh_cm\n1;1;22,28\n".encode("utf-8")
    headers, rows = read_table("trees.csv", data)
    assert headers == ["plot_code", "tree_code", "dbh_cm"]
    assert rows == [{"plot_code": "1", "tree_code": "1", "dbh_cm": "22,28"}]


def test_read_table_xlsx_first_sheet():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["plot_code", "visit_date", "dbh_cm"])
    sheet.append([1.0, dt.datetime(2024, 3, 15), 22.28])
    buffer = io.BytesIO()
    workbook.save(buffer)
    headers, rows = read_table("trees.xlsx", buffer.getvalue())
    assert headers == ["plot_code", "visit_date", "dbh_cm"]
    assert rows == [{"plot_code": "1", "visit_date": "2024-03-15", "dbh_cm": "22.28"}]


@pytest.mark.parametrize(
    ("filename", "data", "code"),
    [("trees.txt", b"a,b\n1,2\n", "unsupported_file"), ("trees.csv", b"", "empty_file"), ("trees.csv", b"a,b\n", "empty_file")],
)
def test_unsupported_extension_and_empty_file(filename, data, code):
    with pytest.raises(FieldImportError) as info:
        read_table(filename, data)
    assert info.value.code == code


def test_too_many_rows_rejected():
    data = ("a\n" + "1\n" * 50_001).encode()
    with pytest.raises(FieldImportError) as info:
        read_table("trees.csv", data)
    assert info.value.code == "too_many_rows"


def test_valid_row_is_parsed_with_year_precision():
    report = run({})
    assert report.issues == [] and report.rows_total == 1
    tree = report.trees[0]
    assert (tree.plot_code, tree.tree_code, tree.date_precision) == ("1", "1", "year")
    assert tree.visit_date == dt.date(2024, 1, 1)
    assert tree.status == "live"


def test_day_dates_in_both_formats_and_bad_date():
    report = run({"visit_date": "15/03/2024", "tree_code": "1"}, {"visit_date": "2024-03-16", "tree_code": "2"},
                 {"visit_date": "31/31/2024", "tree_code": "3"})
    assert [(t.visit_date, t.date_precision) for t in report.trees] == [(dt.date(2024, 3, 15), "day"), (dt.date(2024, 3, 16), "day")]
    assert codes(report, "error") == [(4, "bad_date")]


def test_missing_plot_code_and_missing_date():
    report = run({"plot_code": ""}, {"tree_code": "2"}, drop=())
    assert codes(report, "error") == [(2, "missing_plot_code")]
    missing = run({}, drop=("survey_year",))
    assert codes(missing, "error") == [(2, "missing_date")]
    defaulted = run({}, drop=("survey_year",), options=ImportOptions(height_unit="m", default_visit_year=2022))
    assert defaulted.issues == [] and defaulted.trees[0].visit_date == dt.date(2022, 1, 1)


def test_comma_decimals_and_thousands_separators():
    report = run({"dbh_cm": "22,28", "biomass_kg": "1.234,5"})
    assert report.issues == []
    assert report.trees[0].dbh_cm == pytest.approx(22.28)
    assert report.trees[0].biomass_kg == pytest.approx(1234.5)


def test_dash_placeholder_is_missing_not_zero():
    report = run({"dbh_cm": "-", "biomass_kg": "-"})
    tree = report.trees[0]
    assert tree.dbh_cm is None and tree.biomass_kg is None and tree.circumference_cm == 70.0
    assert report.issues == []


def test_bad_and_negative_numbers():
    report = run({"dbh_cm": "abc"}, {"tree_code": "2", "dbh_cm": "-5"})
    assert codes(report, "error") == [(2, "bad_number"), (3, "negative_value")]
    assert report.trees == []


def test_missing_size_is_error():
    report = run({"circumference_cm": "", "dbh_cm": ""})
    assert codes(report, "error") == [(2, "missing_size")]


def test_dbh_circumference_mismatch_is_warning_and_row_kept():
    report = run({"circumference_cm": "70", "dbh_cm": "30"})
    assert codes(report) == [(2, "dbh_circumference_mismatch")]
    assert report.issues[0].level == "warning" and len(report.trees) == 1
    assert "dbh_circumference_mismatch" in report.trees[0].flags


def test_coordinates_validation():
    ok = run({"latitude": "-7.0527", "longitude": "110.4396"})
    assert ok.issues == [] and (ok.trees[0].latitude, ok.trees[0].longitude) == (-7.0527, 110.4396)
    out_of_range = run({"latitude": "91", "longitude": "110"})
    swapped = run({"latitude": "110.43", "longitude": "-7.05"})
    half = run({"latitude": "-7.05"})
    assert codes(out_of_range, "error") == [(2, "bad_coordinates")]
    assert codes(swapped, "error") == [(2, "bad_coordinates")]
    assert codes(half, "error") == [(2, "incomplete_coordinates")]


def test_height_cm_converted_to_metres():
    report = run({"height": "892"}, options=ImportOptions(height_unit="cm"))
    assert report.trees[0].height_m == pytest.approx(8.92)
    assert report.issues == []


def test_height_that_looks_like_wrong_unit_is_warned_not_converted():
    report = run({"height": "892"}, options=ImportOptions(height_unit="m"))
    assert report.trees[0].height_m == 892.0
    assert codes(report) == [(2, "height_suspect_unit")]


def test_status_validation_and_default():
    report = run({"status": "dead"}, {"tree_code": "2", "status": "zombie"})
    assert report.trees[0].status == "dead"
    assert codes(report, "error") == [(3, "bad_status")]


def test_missing_tree_code_is_generated_with_warning():
    report = run({"tree_code": ""})
    assert report.trees[0].tree_code == "row-2"
    assert codes(report) == [(2, "tree_code_generated")]


def test_duplicate_tree_in_same_visit_flags_later_row():
    report = run({}, {})
    assert codes(report, "error") == [(3, "duplicate_row")]
    assert len(report.trees) == 1 and report.error_rows == {3}


def test_plot_area_basis_row_value_is_measured_option_is_assumed():
    measured = run({"plot_area_m2": "625"})
    assumed = run({}, options=ImportOptions(height_unit="m", plot_area_m2=400.0))
    assert (measured.trees[0].area_m2, measured.trees[0].area_basis) == (625.0, "measured")
    assert (assumed.trees[0].area_m2, assumed.trees[0].area_basis) == (400.0, "assumed")
    none = run({})
    assert (none.trees[0].area_m2, none.trees[0].area_basis) == (None, None)


# --- hardening found in the branch review ---------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("plot_code", "p" * 65), ("tree_code", "t" * 65), ("species", "s" * 256), ("operator", "o" * 129)],
)
def test_values_longer_than_the_columns_are_row_errors(field, value):
    report = run({field: value})
    assert codes(report, "error") == [(2, "value_too_long")]
    assert report.trees == []


def test_nul_character_is_a_row_error_not_a_database_error():
    report = run({"species": "Asem\x00Jawa"})
    assert codes(report, "error") == [(2, "bad_text")]


@pytest.mark.parametrize("year", ["inf", "Infinity", "1e999", "nan", "2024.7", "twenty"])
def test_survey_year_must_be_a_finite_whole_year(year):
    report = run({"survey_year": year})
    assert codes(report, "error") == [(2, "bad_date")]


def test_year_written_as_float_from_a_spreadsheet_is_accepted():
    report = run({"survey_year": "2024.0"})
    assert report.issues == [] and report.trees[0].visit_date == dt.date(2024, 1, 1)


@pytest.mark.parametrize("text", ["1.500", "1,234", "12.500"])
def test_ambiguous_thousands_or_decimal_format_is_warned(text):
    report = run({"biomass_kg": text})
    assert [i.code for i in report.issues] == ["ambiguous_number_format"]
    assert report.issues[0].level == "warning" and "ambiguous_number_format" in report.trees[0].flags


@pytest.mark.parametrize("text", ["0.125", "22.28", "205.4233029", "1.234,5", "1500", "1,5"])
def test_unambiguous_numbers_have_no_warning(text):
    assert run({"biomass_kg": text}).issues == []


def test_coordinates_with_three_decimals_are_not_flagged_as_ambiguous():
    report = run({"latitude": "-7.052", "longitude": "110.439"})
    assert report.issues == []


def test_xlsx_row_limit_is_enforced_while_reading(monkeypatch):
    from app.services.dmrv import field_import

    monkeypatch.setattr(field_import, "MAX_ROWS", 5)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["plot_code"])
    for index in range(10):
        sheet.append([index])
    buffer = io.BytesIO()
    workbook.save(buffer)
    with pytest.raises(FieldImportError) as info:
        read_table("big.xlsx", buffer.getvalue())
    assert info.value.code == "too_many_rows"


def test_csv_with_an_enormous_cell_is_a_clean_error():
    with pytest.raises(FieldImportError) as info:
        read_table("trees.csv", b"a\n" + b"x" * 300_000 + b"\n")
    assert info.value.code == "bad_csv"


def test_too_many_columns_is_rejected():
    header = ",".join(f"c{i}" for i in range(300))
    with pytest.raises(FieldImportError) as info:
        read_table("trees.csv", (header + "\n" + ",".join("1" for _ in range(300)) + "\n").encode())
    assert info.value.code == "too_many_columns"
