from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes.carbon import export_carbon_statistics_csv
from app.core.security import User
from app.services.carbon_snapshot_service import (
    build_statistics_csv,
    create_carbon_snapshot,
    safe_csv_filename,
    snapshot_sha256,
)


def _snapshot() -> dict:
    return {
        "schema_version": "savegeo-carbon-analysis-snapshot/v1",
        "analysis_id": "f1a7d66c-91bb-4d77-a599-f53488473709",
        "analysis_type": "carbon",
        "created_at": "2026-11-28T00:00:00+00:00",
        "parameters": {"aoi_name": "Kawasan Uji", "year": 2025, "scale": 30},
        "provenance": {"id": "f1a7d66c-91bb-4d77-a599-f53488473709"},
        "result": {
            "carbon_estimated": {
                "statistics": {"mean": 12.5, "min": 2.0, "max": 28.0, "std_dev": 3.0},
                "unit": "Mg C/ha",
                "target_pool": "aboveground_biomass_carbon",
            },
            "carbon_reference": {"name": "Reference raster", "year": 2020, "resolution": 30},
            "area_info": {"area_ha": 10.0, "total_carbon_tons": 125.0},
            "model_info": {
                "model_name": "model-a",
                "model_version": "2.1",
                "validation_type": "spatial_reference_holdout",
                "validation_metrics": {"rmse": 4.0, "mae": 2.0, "bias": -1.0, "r2": -0.2},
                "validation_samples": 12,
            },
            "data_quality": {"valid_pixel_pct": 83.0, "images_used": 4},
        },
    }


def test_csv_uses_saved_values_units_and_preserves_negative_metrics():
    rows = list(csv.DictReader(io.StringIO(build_statistics_csv(_snapshot()).decode("utf-8-sig"))))
    values = {(row["pool"], row["field"]): row for row in rows}

    assert values[("aboveground_biomass_carbon", "statistics.mean")]["value"] == "12.5"
    assert values[("aboveground_biomass_carbon", "statistics.mean")]["unit"] == "Mg C/ha"
    assert values[("aboveground_biomass_carbon", "area.total_carbon_tons")]["value"] == "125"
    assert values[("aboveground_biomass_carbon", "validation.r2")]["value"] == "-0.2"
    assert values[("aboveground_biomass_carbon", "validation.type")]["value"] == "spatial_reference_holdout"
    assert values[("aboveground_biomass_carbon", "quality.valid_pixel_pct")]["value"] == "83"


def test_csv_marks_unavailable_values_instead_of_filling_zero():
    snapshot = _snapshot()
    snapshot["result"]["data_quality"] = {"valid_pixel_pct": None}
    snapshot["result"]["carbon_estimated"]["statistics"].pop("min")
    rows = list(csv.DictReader(io.StringIO(build_statistics_csv(snapshot).decode("utf-8-sig"))))
    values = {(row["pool"], row["field"]): row for row in rows}

    assert values[("aboveground_biomass_carbon", "quality.valid_pixel_pct")]["status"] == "not_available"
    assert values[("aboveground_biomass_carbon", "statistics.unavailable_fields")]["value"]
    assert ("aboveground_biomass_carbon", "statistics.min") not in values
    assert all(row["value"] != "0" for row in rows if row["status"] == "not_available")


def test_snapshot_is_idempotent_and_keeps_first_result():
    saved = {}

    class FakeDb:
        def get(self, _model, key):
            return saved.get(key)

        def add(self, item):
            saved[item.analysis_id] = item

        def commit(self):
            return None

        def refresh(self, _item):
            return None

    provenance = SimpleNamespace(parameters={"aoi_name": "Kawasan Uji"}, to_dict=lambda: {"id": "run-id"})
    first = create_carbon_snapshot(
        FakeDb(), analysis_id="run-id", owner_key="user:1", analysis_type="carbon",
        result=_snapshot()["result"], provenance=provenance,
    )
    second = create_carbon_snapshot(
        FakeDb(), analysis_id="run-id", owner_key="user:1", analysis_type="carbon",
        result={"carbon_estimated": {"statistics": {"mean": 999}}}, provenance=provenance,
    )

    assert first is second
    assert first.snapshot["result"]["carbon_estimated"]["statistics"]["mean"] == 12.5
    assert first.sha256 == snapshot_sha256(first.snapshot)


def test_csv_route_hides_snapshot_from_another_account(monkeypatch):
    from app.db.models.carbon_analysis_snapshot import CarbonAnalysisSnapshot

    document = _snapshot()
    saved = CarbonAnalysisSnapshot(
        analysis_id=document["analysis_id"], owner_key="user:11", snapshot=document,
        sha256=snapshot_sha256(document),
    )
    db = SimpleNamespace(get=lambda _model, _key: saved)
    viewer = User(id=12)

    with pytest.raises(HTTPException) as error:
        export_carbon_statistics_csv(document["analysis_id"], viewer=viewer, db=db)

    assert error.value.status_code == 404


def test_csv_route_returns_saved_snapshot_for_owner(monkeypatch):
    from app.db.models.carbon_analysis_snapshot import CarbonAnalysisSnapshot

    document = _snapshot()
    saved = CarbonAnalysisSnapshot(
        analysis_id=document["analysis_id"], owner_key="user:11", snapshot=document,
        sha256=snapshot_sha256(document),
    )
    db = SimpleNamespace(get=lambda _model, _key: saved)
    response = export_carbon_statistics_csv(document["analysis_id"], viewer=User(id=11), db=db)

    assert response.media_type.startswith("text/csv")
    assert "savegeo_carbon_Kawasan_Uji.csv" in response.headers["content-disposition"]
    assert response.body.startswith(b"\xef\xbb\xbf")


def test_filename_sanitizes_path_and_control_characters():
    assert safe_csv_filename("../../Aoi: test\n", "12345678-uuid") == "savegeo_carbon_Aoi_test.csv"
