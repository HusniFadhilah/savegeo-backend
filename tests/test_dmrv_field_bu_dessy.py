"""End-to-end field-data import using the Bu Dessy survey (Tembalang, Point ID 1).

bu_dessy_sample.csv holds the 21 real tree rows of Point ID 1 (source headers kept,
including the junk trailing columns) plus Tahun/Latitude/Longitude columns taken
from the survey's coordinate table. bu_dessy_sample_synthetic_2022.csv is SYNTHETIC:
three trees of the same point, shrunk, used only to exercise repeat-visit history.
"""
import csv
from pathlib import Path

import pytest

from tests.test_dmrv_projects import call, create, env  # noqa: F401  (env is a fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "dmrv"
SAMPLE = FIXTURES / "bu_dessy_sample.csv"
SYNTHETIC_2022 = FIXTURES / "bu_dessy_sample_synthetic_2022.csv"
ASSUMED_PARAMS = {"height_unit": "cm", "plot_area_m2": "400", "area_basis": "assumed"}


def post(client, pid, suffix, path, *, key=None, **form):
    return call(
        client, "POST", f"/dmrv/projects/{pid}/field-data/{suffix}", key=key,
        files={"file": (path.name, path.read_bytes(), "text/csv")}, data=form,
    )


def source_biomass_kg(path) -> list[float]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [float(row["Biomassa (kg)"]) for row in csv.DictReader(handle)]


def test_preview_maps_the_survey_headers_without_manual_work(env):  # noqa: F811
    client, _ = env
    pid = create(client).json()["id"]
    body = post(client, pid, "imports/preview", SAMPLE, **ASSUMED_PARAMS).json()
    assert body["suggested_mapping"] == {
        "plot_code": "Point ID", "survey_year": "Tahun", "tree_code": "No.", "species": "Jenis Pohon",
        "circumference_cm": "Keliling (cm)", "dbh_cm": "Diameter (cm)", "height": "Tinggi(cm)",
        "biomass_kg": "Biomassa (kg)", "latitude": "Latitude", "longitude": "Longitude",
    }
    assert body["report"]["rows_total"] == 21 and body["report"]["errors"] == 0


def test_survey_imports_then_revisit_keeps_history_and_reports_assumed_area(env):  # noqa: F811
    client, _ = env
    pid = create(client).json()["id"]
    first = post(client, pid, "imports", SAMPLE, key="bd-2024", mode="atomic", **ASSUMED_PARAMS)
    assert first.status_code == 201, first.text
    assert (first.json()["status"], first.json()["rows_imported"]) == ("committed", 21)
    revisit = post(client, pid, "imports", SYNTHETIC_2022, key="bd-2022", mode="atomic", **ASSUMED_PARAMS)
    assert revisit.status_code == 201 and revisit.json()["visits_created"] == 1 and revisit.json()["plots_created"] == 0

    plots = call(client, "GET", f"/dmrv/projects/{pid}/field-data/plots").json()
    assert [(p["plot_code"], p["visit_count"], p["area_basis"], p["latitude"]) for p in plots] == [
        ("1", 2, "assumed", pytest.approx(-7.052722222222222))
    ]
    visits = call(client, "GET", f"/dmrv/projects/{pid}/field-data/plots/1/visits").json()
    assert [(v["visit_date"], v["date_precision"], v["tree_count"]) for v in visits] == [
        ("2022-01-01", "year", 3), ("2024-01-01", "year", 21),
    ]
    density_2024 = visits[1]["density"]
    expected = sum(source_biomass_kg(SAMPLE)) / 1000 / (400 / 10_000) * 0.47
    assert density_2024["carbon_mg_c_per_ha"] == pytest.approx(expected, rel=1e-9)
    assert density_2024["area_basis"] == "assumed" and density_2024["source_type"] == "field_measurement"

    again = post(client, pid, "imports", SAMPLE, key="bd-again", mode="atomic", **ASSUMED_PARAMS).json()
    assert again["status"] == "rejected" and {i["code"] for i in again["issues"] if i["level"] == "error"} == {"visit_exists"}


def test_wrong_height_unit_is_flagged_instead_of_silently_accepted(env):  # noqa: F811
    client, _ = env
    pid = create(client).json()["id"]
    body = post(client, pid, "imports/preview", SAMPLE, height_unit="m").json()
    warnings = [i for i in body["report"]["issues"] if i["code"] == "height_suspect_unit"]
    assert len(warnings) == 21 and body["report"]["errors"] == 0
