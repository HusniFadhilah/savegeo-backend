import datetime as dt
import hashlib
import io
import json
import zipfile

import pytest
from sqlalchemy import select

from app.db.base import Base
from app.db.models.dmrv_advanced import DmrvCreditEvent, DmrvFieldCapture
from app.db.models.dmrv_integrity import (
    DmrvIntegrityDocument,
    DmrvIntegrityRecord,
    DmrvIntegrityRevision,
    DmrvRiskPolicy,
)
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.organization import Organization, OrganizationMember
from app.schemas.dmrv_integrity import AccountingInput, ValidationInput
from app.services.dmrv.integrity_calculations import spatial_validation, stock_change
from app.services.dmrv.integrity_risks import enqueue_due
from app.workers.dmrv_worker import run_one
from tests.test_dmrv_projects import SQUARE, call, create, env as dmrv_env  # noqa: F401 - pytest fixture

EXTRA = [
    DmrvIntegrityRecord.__table__,
    DmrvIntegrityRevision.__table__,
    DmrvIntegrityDocument.__table__,
    DmrvRiskPolicy.__table__,
    DmrvCreditEvent.__table__,
    DmrvFieldCapture.__table__,
]


@pytest.fixture
def ienv(dmrv_env, tmp_path, monkeypatch):  # noqa: F811 - imported pytest fixture
    client, factory = dmrv_env
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine, tables=EXTRA)
    monkeypatch.setenv("DMRV_INTEGRITY_DIR", str(tmp_path / "documents"))
    yield client, factory
    Base.metadata.drop_all(engine, tables=EXTRA)


def setup_project(client, *, boundary=True, reviewer=True):
    project_id = create(client).json()["id"]
    if boundary:
        assert (
            call(
                client,
                "POST",
                f"/dmrv/projects/{project_id}/boundaries",
                json={"geojson": SQUARE, "reason": "fixture"},
            ).status_code
            == 201
        )
    if reviewer:
        assert (
            call(
                client,
                "POST",
                f"/dmrv/projects/{project_id}/members",
                json={"principal_type": "admin", "username": "owner-b", "role": "reviewer"},
            ).status_code
            == 201
        )
    return project_id, f"/dmrv/projects/{project_id}/integrity"


def save(
    client, base, category="readiness", key="legal_entity", revision=0, status="submitted", payload=None
):
    return call(
        client,
        "PUT",
        f"{base}/records/{category}/{key}",
        if_match=f'"{revision}"',
        json={
            "title": "Evidence",
            "status": status,
            "payload": payload
            or {"summary": "Source document", "evidence": ["https://example.org/evidence.pdf"]},
        },
    )


def test_readiness_independent_review_stale_write_history_and_export(ienv):
    client, _ = ienv
    project_id, base = setup_project(client)
    first = save(client, base)
    assert first.status_code == 200, first.text
    record = first.json()
    assert call(client, "GET", base).json()["readiness"]["score_pct"] == 0
    assert (
        call(
            client,
            "POST",
            f"{base}/records/{record['id']}/review",
            if_match='"1"',
            json={"decision": "accepted", "reason": "review passed"},
        ).status_code
        == 403
    )
    accepted = call(
        client,
        "POST",
        f"{base}/records/{record['id']}/review",
        viewer="admin:2",
        if_match='"1"',
        json={"decision": "accepted", "reason": "Source checked independently"},
    )
    assert accepted.status_code == 200, accepted.text
    assert call(client, "GET", base).json()["readiness"]["score_pct"] == 6.2
    assert save(client, base, revision=1).status_code == 412
    assert save(client, base, revision=2, status="draft").status_code == 200
    history = call(client, "GET", f"{base}/records/{record['id']}/history").json()
    assert [h["revision"] for h in history] == [3, 2, 1]
    for h in history:
        raw = json.dumps(
            h["snapshot"], sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        assert hashlib.sha256(raw).hexdigest() == h["sha256"]
    exported = call(client, "GET", f"{base}/export")
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for entry in manifest["entries"]:
            assert hashlib.sha256(archive.read(entry["path"])).hexdigest() == entry["sha256"]
    assert call(client, "GET", "/dmrv/portfolio").json()["items"][0]["id"] == project_id


def test_membership_is_enforced_for_all_new_surfaces(ienv):
    client, _ = ienv
    _, base = setup_project(client, reviewer=False)
    assert call(client, "GET", base, viewer="admin:2").status_code == 404
    assert call(client, "GET", f"{base}/export", viewer="admin:2").status_code == 404
    assert call(client, "GET", "/dmrv/portfolio", viewer="admin:2").json()["total"] == 0
    assert save(client, base, status="submitted", payload={"summary": "No evidence"}).status_code == 422
    assert (
        call(client, "GET", base.replace(base.split("/")[3], "unknown"), viewer="admin:2").status_code == 404
    )


def test_required_criterion_cannot_be_excluded_and_field_role_cannot_write(ienv):
    client, _ = ienv
    project_id, base = setup_project(client)
    record = save(client, base).json()
    assert (
        call(
            client,
            "POST",
            f"{base}/records/{record['id']}/review",
            viewer="admin:2",
            if_match='"1"',
            json={"decision": "not_applicable", "reason": "not applicable here"},
        ).status_code
        == 422
    )
    record = save(client, base, category="safeguard", key="cultural_sites").json()
    assert (
        call(
            client,
            "POST",
            f"{base}/records/{record['id']}/review",
            viewer="admin:2",
            if_match='"1"',
            json={"decision": "not_applicable", "reason": "Survey found no cultural sites"},
        ).status_code
        == 200
    )
    call(
        client,
        "POST",
        f"/dmrv/projects/{project_id}/members",
        json={"principal_type": "user", "username": "field-user", "role": "field_operator"},
    )
    assert call(client, "GET", base, viewer="user:1").status_code == 403
    assert call(client, "GET", "/dmrv/portfolio", viewer="user:1").json()["total"] == 0


def accounting_payload(period_id="period"):
    return {
        "period_id": period_id,
        "methodology_id": "VM0047",
        "methodology_version": "1.1",
        "profile_reference": "https://example.org/methodology",
        "rule_justification": "Documented screening rules for review",
        "pools": [
            {"pool": "AGB", "project_start": 10, "project_end": 20, "baseline_start": 10, "baseline_end": 12}
        ],
        "excluded_pools_reason": "Other pools not measured in this fixture",
        "project_emissions_tco2e": 5,
        "leakage_tco2e": 3,
        "uncertainty_deduction_pct": 10,
        "buffer_pct": 20,
        "evidence": ["https://example.org/calculation"],
    }


def test_accounting_exact_deductions_missing_input_and_negative_reversal():
    payload = accounting_payload()
    result = stock_change(AccountingInput(**payload), 10)
    net = 80 * 44 / 12 - 8
    assert result["net_mitigation_tco2e"] == pytest.approx(net)
    assert result["scenario_units_tco2e"] == pytest.approx(net * 0.9 * 0.8)
    assert result["issuance_status"] == "not_issued"
    payload["leakage_tco2e"] = None
    result = stock_change(AccountingInput(**payload), 10)
    assert result["net_mitigation_tco2e"] is None and result["scenario_units_tco2e"] is None
    payload["leakage_tco2e"] = 3
    payload["pools"][0]["project_end"] = 5
    result = stock_change(AccountingInput(**payload), 10)
    assert result["net_mitigation_tco2e"] < 0 and result["reversal_tco2e"] > 0
    assert result["scenario_units_tco2e"] == 0


def test_accounting_uses_pinned_period_boundary_and_rejects_wrong_method(ienv):
    client, _ = ienv
    project_id, base = setup_project(client)
    period = call(
        client,
        "POST",
        f"/dmrv/projects/{project_id}/monitoring-periods",
        key="period",
        json={"name": "2025", "start_date": "2025-01-01", "end_date": "2025-12-31", "boundary_version": 1},
    ).json()
    payload = accounting_payload(period["id"])
    result = save(client, base, "accounting", "calculation", payload=payload)
    assert result.status_code == 200, result.text
    assert result.json()["result"]["boundary_version"] == 1
    assert result.json()["result"]["area_ha"] > 100
    payload["methodology_version"] = "wrong"
    assert save(client, base, "accounting", "wrong", payload=payload).status_code == 422


def test_registry_conflicts_anonymized_and_spatial_overlap_is_screening(ienv):
    client, _ = ienv
    _, base = setup_project(client)
    second = create(
        client, viewer="admin:2", key="other-project", payload={"name": "Private project B"}
    ).json()["id"]
    otherbase = f"/dmrv/projects/{second}/integrity"
    payload = {
        "registry": "Example",
        "external_project_id": "public123",
        "registry_url": "https://example.org/registry",
        "vintage_start": "2024-01-01",
        "vintage_end": "2024-12-31",
        "serial_prefix": "AB",
        "serial_start": 100,
        "serial_end": 200,
        "unit_status": "reported_issued",
        "checked_on": "2026-01-01",
        "evidence": ["https://example.org/proof"],
    }
    assert save(client, base, "registry", "units", payload=payload).status_code == 200
    response = call(
        client,
        "PUT",
        f"{otherbase}/records/registry/units",
        viewer="admin:2",
        if_match='"0"',
        json={"title": "Private serials", "status": "submitted", "payload": payload},
    )
    assert response.status_code == 200, response.text
    result = call(client, "GET", base).json()["registry_check"]
    assert result["serial_conflicts"][0]["conflicting_records"] == 1
    assert result["external_registry_status"] == "not_connected"
    assert second not in json.dumps(result) and "Private" not in json.dumps(result)


def validation_payload():
    pairs = [
        {
            "point_id": f"p{i}",
            "lon": 108 + (i % 8) * 0.3,
            "lat": -8 + (i // 8) * 0.3,
            "observed": 2 + i * 0.8,
            "predicted": float(i + 1),
        }
        for i in range(48)
    ]
    return {
        "pool": "AGB",
        "product": "Test reference",
        "reference_year": 2025,
        "predictor_year": 2025,
        "label_source": "field_measurements",
        "unit": "Mg_C_ha",
        "provenance": "Independent measured field pairs for calibration test",
        "pairs": pairs,
        "block_degrees": 0.1,
        "buffer_km": 10,
    }


def test_buffered_spatial_calibration_and_negative_r2_are_preserved():
    data = validation_payload()
    result = spatial_validation(ValidationInput(**data))
    assert result["calibration_status"] == "completed"
    assert result["calibrated_oof"]["r2"] > 0.999
    assert all(f["train_n"] >= 5 for f in result["folds"])
    assert result["calibrated_bootstrap"]["replicates"] == 300
    for pair in data["pairs"]:
        pair["predicted"] = 5000
    result = spatial_validation(ValidationInput(**data))
    assert result["raw_map"]["r2"] < 0
    assert result["calibrated_oof"] is None
    data["pairs"][1]["lon"] = data["pairs"][0]["lon"]
    data["pairs"][1]["lat"] = data["pairs"][0]["lat"]
    with pytest.raises(ValueError):
        ValidationInput(**data)


def test_documents_checksum_access_and_export(ienv):
    client, _ = ienv
    _, base = setup_project(client, reviewer=False)
    uploaded = call(
        client,
        "POST",
        f"{base}/documents",
        files={"file": ("../../proof.txt", b"Evidence text", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    assert document["filename"] == "proof.txt"
    assert (
        call(client, "GET", f"{base}/documents/{document['id']}/download", viewer="admin:2").status_code
        == 404
    )
    assert call(client, "GET", f"{base}/documents/{document['id']}/download").content == b"Evidence text"
    assert (
        save(
            client, base, payload={"summary": "Uploaded proof", "evidence": [f"document:{document['id']}"]}
        ).status_code
        == 200
    )
    assert (
        save(
            client, base, key="baseline", payload={"summary": "Wrong proof", "evidence": ["document:unknown"]}
        ).status_code
        == 422
    )
    assert (
        call(
            client,
            "POST",
            f"{base}/documents",
            files={"file": ("proof.pdf", b"not a pdf", "application/pdf")},
        ).status_code
        == 422
    )


def test_scheduled_scan_is_pinned_deduplicated_and_worker_emits_reviewable_risks(ienv, monkeypatch):
    client, factory = ienv
    project_id, base = setup_project(client)
    policy = call(client, "PUT", f"{base}/risk-policy", if_match='"0"', json={"enabled": True})
    assert policy.status_code == 200, policy.text
    with factory() as db:
        assert enqueue_due(db, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)) == 1
        assert enqueue_due(db, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=6)) == 0
    monkeypatch.setattr("app.services.gee_service.initialize_ee", lambda db: True)
    monkeypatch.setattr(
        "app.services.dmrv.integrity_risks.earth_engine_signals",
        lambda geometry, payload: {
            "supported_area_ha": 0,
            "potential_tree_cover_change_ha": 3,
            "high_confidence_fire_raster_cells": 1,
            "before_scenes": 2,
            "after_scenes": 2,
            "fire_scenes": 20,
        },
    )
    assert run_one(factory, "integrity-test-worker") == "succeeded"
    data = call(client, "GET", base).json()
    risks = [r for r in data["records"] if r["category"] == "risk"]
    assert len(risks) == 3 and all(r["status"] == "open" for r in risks)
    assert all(r["result"]["boundary_version"] == 1 for r in risks)
    assert call(client, "GET", "/dmrv/portfolio").json()["summary"]["open_risks"] == 3


def test_grievance_lifecycle_retains_followup(ienv):
    client, _ = ienv
    _, base = setup_project(client)
    response = save(
        client,
        base,
        "grievance",
        "case001",
        status="open",
        payload={
            "summary": "Anonymous access issue",
            "evidence": [],
            "topic": "access",
            "severity": "medium",
            "received_on": "2026-01-01",
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()
    closed = call(
        client,
        "POST",
        f"{base}/records/{record['id']}/review",
        if_match='"1"',
        json={"decision": "resolved", "reason": "Access restored and documented"},
    )
    assert closed.status_code == 200 and closed.json()["status"] == "resolved"


def test_csv_validation_summary_is_lightweight_and_full_data_is_retained(ienv):
    client, _ = ienv
    _, base = setup_project(client)
    metadata = validation_payload()
    pairs = metadata.pop("pairs")
    content = "point_id,lon,lat,observed,predicted\n" + "\n".join(
        f"{p['point_id']},{p['lon']},{p['lat']},{p['observed']},{p['predicted']}" for p in pairs
    )
    response = call(
        client,
        "POST",
        f"{base}/validation-import",
        if_match='"0"',
        data={"key": "validation", "metadata": json.dumps(metadata)},
        files={"file": ("pairs.csv", content.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["result"]["calibration_status"] == "completed"
    assert "validation_points_outside_project" in record["result"]["warnings"]
    summary = next(r for r in call(client, "GET", base).json()["records"] if r["id"] == record["id"])
    assert "pairs" not in summary["payload"] and "oof" not in summary["result"]
    full = call(client, "GET", f"{base}/records/{record['id']}").json()
    assert len(full["payload"]["pairs"]) == 48 and len(full["result"]["oof"]) == 48


def test_membership_revocation_removes_portfolio_access_and_preserves_owner(ienv):
    client, _ = ienv
    project_id, base = setup_project(client)
    assert call(client, "GET", "/dmrv/portfolio", viewer="admin:2").json()["total"] == 1
    assert call(client, "GET", f"{base}/members", viewer="admin:2").status_code == 403
    assert call(client, "DELETE", f"{base}/members/admin/1").status_code == 422
    assert call(client, "DELETE", f"{base}/members/admin/2").status_code == 200
    assert call(client, "GET", "/dmrv/portfolio", viewer="admin:2").json()["total"] == 0
    assert call(client, "GET", base, viewer="admin:2").status_code == 404


def test_role_change_does_not_allow_an_author_to_review_their_own_evidence(ienv):
    client, factory = ienv
    project_id, base = setup_project(client)
    record = save(client, base).json()
    with factory() as db:
        member = db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.principal_type == "admin",
                ProjectMember.principal_id == 1,
            )
        ).scalar_one()
        member.role = "reviewer"
        db.commit()
    response = call(
        client,
        "POST",
        f"{base}/records/{record['id']}/review",
        if_match='"1"',
        json={"decision": "accepted", "reason": "Trying to self-review"},
    )
    assert response.status_code == 403 and response.json()["code"] == "independent_reviewer_required"


def test_satellite_failure_stays_retryable_and_does_not_create_zero_risk_evidence(ienv, monkeypatch):
    client, factory = ienv
    _, base = setup_project(client)
    first = call(client, "POST", f"{base}/risk-scans", key="scan-key", json={})
    replay = call(client, "POST", f"{base}/risk-scans", key="scan-key", json={})
    assert first.status_code == replay.status_code == 202 and first.json()["id"] == replay.json()["id"]
    monkeypatch.setattr("app.services.gee_service.initialize_ee", lambda db: True)

    def fail(*args):
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr("app.services.dmrv.integrity_risks.earth_engine_signals", fail)
    assert run_one(factory, "failing-risk-worker") == "queued"
    assert call(client, "GET", base).json()["records"] == []


def test_corrupted_history_blocks_export(ienv):
    client, factory = ienv
    _, base = setup_project(client)
    record = save(client, base).json()
    with factory() as db:
        revision = db.execute(
            select(DmrvIntegrityRevision).where(DmrvIntegrityRevision.record_id == record["id"])
        ).scalar_one()
        revision.snapshot = {"altered": True}
        db.commit()
    assert call(client, "GET", f"{base}/records/{record['id']}/history").status_code == 409
    assert call(client, "GET", f"{base}/export").status_code == 409


def test_organization_revocation_removes_integrity_and_portfolio_access(ienv):
    client, factory = ienv
    project_id, base = setup_project(client)
    with factory() as db:
        db.add(Organization(id="org-integrity", name="Research", created_by="admin:1"))
        db.flush()
        db.add(
            OrganizationMember(
                id="org-reviewer",
                organization_id="org-integrity",
                principal_type="admin",
                principal_id=2,
                role="member",
                is_active=True,
            )
        )
        db.get(DmrvProject, project_id).organization_id = "org-integrity"
        db.commit()
    assert call(client, "GET", "/dmrv/portfolio", viewer="admin:2").json()["total"] == 1
    assert call(client, "GET", base, viewer="admin:2").status_code == 200
    with factory() as db:
        db.get(OrganizationMember, "org-reviewer").is_active = False
        db.commit()
    assert call(client, "GET", "/dmrv/portfolio", viewer="admin:2").json()["total"] == 0
    assert call(client, "GET", base, viewer="admin:2").status_code == 404
    assert call(client, "GET", f"{base}/export", viewer="admin:2").status_code == 404
