from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.routes import carbon_calibration
from app.schemas.carbon_calibration import CalibrationDatasetCreate


def _payload(**overrides):
    value = {
        "dataset_id": "undip_carbon_reference_2026",
        "name": "UNDIP Carbon Stock Reference 2026",
        "plot_count": 0,
        "plot_size_m": [],
        "sample_area_ha": 0,
        "source_reference_only": True,
        "source_references": [
            {
                "label": "Source workbook",
                "url": "https://drive.google.com/file/d/example/view",
            }
        ],
        "reported_2026_summary": {
            "agb_ton_c_workbook": 23712.35,
            "soc_ton_c_workbook": 1623742.67,
            "summary_use": "context_only_not_training_labels",
        },
        "calibration_readiness": {
            "plot_observations_imported": False,
            "plot_area_confirmed": False,
            "coordinate_review_required": True,
            "soc_totals_reconciled": False,
            "unsupported_pools": ["belowground_biomass_carbon"],
        },
    }
    value.update(overrides)
    return value


def test_reference_dataset_manifest_keeps_typed_source_metadata():
    payload = CalibrationDatasetCreate.model_validate(_payload())

    manifest = payload.manifest()
    assert manifest["source_reference_only"] is True
    assert manifest["source_references"][0]["url"] == "https://drive.google.com/file/d/example/view"
    assert manifest["reported_2026_summary"]["summary_use"] == "context_only_not_training_labels"
    assert manifest["calibration_readiness"]["plot_observations_imported"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://drive.google.com/file/d/example/view",
        "https://example.org/attacker-controlled",
        "https://user:password@drive.google.com/file/d/example/view",
    ],
)
def test_reference_dataset_rejects_unapproved_or_unsafe_source_links(url: str):
    with pytest.raises(ValidationError):
        CalibrationDatasetCreate.model_validate(_payload(source_references=[{"label": "Source", "url": url}]))


def test_source_reference_draft_cannot_claim_unimported_plots():
    with pytest.raises(ValidationError, match="cannot claim unimported field plots"):
        CalibrationDatasetCreate.model_validate(_payload(plot_count=10))


def test_source_reference_draft_requires_source_link():
    with pytest.raises(ValidationError, match="must include at least one reviewed source link"):
        CalibrationDatasetCreate.model_validate(_payload(source_references=[]))


def test_source_reference_dataset_without_reviewed_plots_stays_blocked(monkeypatch):
    dataset = SimpleNamespace(
        manifest={"source_reference_only": True},
        plots=[],
        files=[],
        validation_report=None,
        status="validated",
    )

    class FakeDB:
        committed = False

        def commit(self):
            self.committed = True

    db = FakeDB()
    monkeypatch.setattr(carbon_calibration, "_dataset_or_404", lambda *_args: dataset)

    result = carbon_calibration.validate_dataset("undip_carbon_reference_2026", db)

    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "field_plots_not_imported"
    assert dataset.status == "draft"
    assert db.committed is True
