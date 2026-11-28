import datetime as dt
import os
import uuid

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateSchema, DropSchema

from app.api.routes import dmrv as dmrv_routes
from app.core.security import get_current_app_viewer
from app.core.viewer_features import DEFAULT_VIEWER_FEATURE_CODES
from app.db.base import Base
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_audit import DmrvAuditEvent, DmrvIdempotencyKey
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_submission import DmrvSubmission, DmrvReviewFinding
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.organization import Organization, OrganizationMember
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.user import User
from app.db.session import get_db
from app.services.dmrv.errors import register_dmrv_error_handler


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


TABLES = [
    User.__table__, AdminUser.__table__, Role.__table__, Permission.__table__, role_permissions,
    Organization.__table__, OrganizationMember.__table__,
    DmrvProject.__table__, ProjectMember.__table__, BoundaryVersion.__table__,
    DmrvJob.__table__, DmrvSnapshot.__table__, DmrvEvidencePackage.__table__,
    DmrvSubmission.__table__, DmrvReviewFinding.__table__,
    DmrvMonitoringPeriod.__table__,
    DmrvAuditEvent.__table__, DmrvIdempotencyKey.__table__,
    ImportBatch.__table__, SurveyPlot.__table__, SurveyVisit.__table__, TreeMeasurement.__table__,
]
PROJECT = {"name": "Pilot ARR", "organization": "UNDIP", "methodology_id": "VM0047", "methodology_version": "1.1"}
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[110.0, -7.0], [110.01, -7.0], [110.01, -6.99], [110.0, -6.99], [110.0, -7.0]]],
}
SQUARE_2 = {
    "type": "Polygon",
    "coordinates": [[[110.0, -7.0], [110.02, -7.0], [110.02, -6.98], [110.0, -6.98], [110.0, -7.0]]],
}


@pytest.fixture()
def env():
    # Set DMRV_TEST_DATABASE_URL to a THROWAWAY PostgreSQL database to run these
    # tests on PostgreSQL; the tables are dropped afterwards. Default: SQLite.
    url = os.environ.get("DMRV_TEST_DATABASE_URL")
    root_engine = None
    schema = None
    if url:
        root_engine = create_engine(url)
        schema = "dmrv_api_test_" + uuid.uuid4().hex
        with root_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as seed:
        seed.add(Role(id=1, name="limited"))
        viewer_role = Role(
            id=2,
            name="viewer",
            is_viewer=True,
            feature_permissions_configured=True,
        )
        viewer_role.permissions = [Permission(code=code) for code in DEFAULT_VIEWER_FEATURE_CODES]
        seed.add(viewer_role)
        seed.add_all([
            User(id=1, username="field-user", email="f@x.test", password_hash="x"),
            AdminUser(id=1, username="owner-a", password_hash="x", is_superuser=True),
            AdminUser(id=2, username="owner-b", password_hash="x", is_superuser=True),
            AdminUser(id=3, username="no-permission", password_hash="x", role_id=1),
        ])
        seed.commit()

    app = FastAPI()
    register_dmrv_error_handler(app)
    app.include_router(dmrv_routes.router)

    def get_test_db():
        with factory() as session:
            yield session

    def test_viewer(request: Request, db: Session = Depends(get_test_db)):
        kind, ident = request.headers["X-Test-Viewer"].split(":")
        return db.get(User if kind == "user" else AdminUser, int(ident))

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_current_app_viewer] = test_viewer
    client = TestClient(app, raise_server_exceptions=False)
    yield client, factory
    if root_engine is not None:
        engine.dispose()
        with root_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        root_engine.dispose()
    engine.dispose()


def call(client, method, url, viewer="admin:1", key=None, if_match=None, **kwargs):
    headers = {"X-Test-Viewer": viewer}
    if key:
        headers["Idempotency-Key"] = key
    if if_match:
        headers["If-Match"] = if_match
    return client.request(method, url, headers=headers, **kwargs)


def count(factory, model) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def create(client, viewer="admin:1", key="k-1", payload=PROJECT):
    return call(client, "POST", "/dmrv/projects", viewer=viewer, key=key, json=payload)


def test_create_project_adds_owner_and_audit_in_one_commit(env):
    client, factory = env
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["revision"] == 1 and body["my_role"] == "project_owner" and body["status"] == "draft"
    assert response.headers["ETag"] == '"1"'
    with factory() as db:
        member = db.execute(select(ProjectMember)).scalar_one()
        assert (member.principal_type, member.principal_id, member.role) == ("admin", 1, "project_owner")
        audit = db.execute(select(DmrvAuditEvent)).scalar_one()
        assert (audit.action, audit.actor_type, audit.actor_id, audit.project_id) == ("project.create", "admin", 1, body["id"])


def test_same_idempotency_key_same_body_returns_same_project(env):
    client, factory = env
    first = create(client)
    second = create(client)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (count(factory, DmrvProject), count(factory, ProjectMember), count(factory, DmrvAuditEvent)) == (1, 1, 1)


def test_same_idempotency_key_different_body_is_422(env):
    client, factory = env
    create(client)
    response = create(client, payload={**PROJECT, "name": "Other"})
    assert response.status_code == 422
    assert response.json()["code"] == "idempotency_key_reused"
    assert count(factory, DmrvProject) == 1


def test_missing_idempotency_key_is_400(env):
    client, _ = env
    response = call(client, "POST", "/dmrv/projects", json=PROJECT)
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"


def test_patch_with_stale_if_match_is_412_and_changes_nothing(env):
    client, factory = env
    project_id = create(client).json()["id"]
    ok = call(client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"', json={"name": "Renamed"})
    assert ok.status_code == 200 and ok.json()["revision"] == 2 and ok.headers["ETag"] == '"2"'
    stale = call(client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"', json={"name": "Lost update"})
    assert stale.status_code == 412
    assert stale.json()["code"] == "revision_mismatch"
    with factory() as db:
        assert db.get(DmrvProject, project_id).name == "Renamed"
        updates = db.execute(select(func.count()).select_from(DmrvAuditEvent).where(DmrvAuditEvent.action == "project.update")).scalar_one()
        assert updates == 1


def test_patch_requires_if_match(env):
    client, _ = env
    project_id = create(client).json()["id"]
    response = call(client, "PATCH", f"/dmrv/projects/{project_id}", json={"name": "x"})
    assert response.status_code == 428


def test_methodology_is_locked_after_monitoring_period_starts(env):
    client, factory = env
    project_id = create(client).json()["id"]
    boundary_id = "boundary-method-lock"
    with factory() as db:
        db.add(BoundaryVersion(
            id=boundary_id, project_id=project_id, version=1, original_geojson=SQUARE,
            validated_geojson=SQUARE, crs="EPSG:4326", area_ha=1.0, area_method="geodesic",
            checksum="b" * 64, created_by="admin:1", reason="method lock test",
        ))
        db.add(DmrvMonitoringPeriod(
            id="period-method-lock", project_id=project_id, boundary_version_id=boundary_id,
            name="Monitoring 2026", start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 12, 31),
            status="in_progress", created_by="admin:1",
        ))
        db.commit()
    response = call(
        client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"',
        json={"methodology_id": "VM0033", "methodology_version": "2.1"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "methodology_locked"
    with factory() as db:
        project = db.get(DmrvProject, project_id)
        assert (project.methodology_id, project.methodology_version) == ("VM0047", "1.1")


def test_user_and_admin_without_permission_cannot_create_project(env):
    client, factory = env
    assert create(client, viewer="user:1", key="a").status_code == 403
    assert create(client, viewer="admin:3", key="b").status_code == 403
    assert count(factory, DmrvProject) == 0


def test_null_role_admin_can_create_but_not_read_others_project(env):
    client, _ = env
    project_id = create(client, viewer="admin:1").json()["id"]
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="admin:2").status_code == 404
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="admin:1").status_code == 200


def test_add_member_rejects_unknown_role_and_unknown_account_then_grants_access(env):
    client, _ = env
    project_id = create(client).json()["id"]
    url = f"/dmrv/projects/{project_id}/members"
    bad_role = call(client, "POST", url, json={"principal_type": "user", "username": "field-user", "role": "king"})
    assert bad_role.status_code == 422 and bad_role.json()["code"] == "invalid_role"
    missing = call(client, "POST", url, json={"principal_type": "user", "username": "ghost", "role": "analyst"})
    assert missing.status_code == 422 and missing.json()["code"] == "unknown_account"
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="user:1").status_code == 404

    added = call(client, "POST", url, json={"principal_type": "user", "username": "field-user", "role": "field_operator"})
    assert added.status_code == 201 and added.json()["role"] == "field_operator"
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="user:1").status_code == 200
    again = call(client, "POST", url, json={"principal_type": "user", "username": "field-user", "role": "analyst"})
    assert again.status_code == 409


def test_boundary_versions_increment_and_old_versions_remain(env):
    client, _ = env
    project_id = create(client).json()["id"]
    url = f"/dmrv/projects/{project_id}/boundaries"
    first = call(client, "POST", url, json={"geojson": SQUARE, "reason": "initial"})
    second = call(client, "POST", url, json={"geojson": SQUARE_2, "reason": "revised after field check"})
    assert (first.status_code, second.status_code) == (201, 201)
    assert (first.json()["version"], second.json()["version"]) == (1, 2)
    listed = call(client, "GET", url).json()
    assert [item["version"] for item in listed] == [1, 2]
    assert listed[0]["checksum"] != listed[1]["checksum"]


def test_invalid_boundary_is_422_with_code(env):
    client, _ = env
    project_id = create(client).json()["id"]
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    response = call(client, "POST", f"/dmrv/projects/{project_id}/boundaries", json={"geojson": bowtie, "reason": "x"})
    assert response.status_code == 422 and response.json()["code"] == "invalid_geometry"


def test_audit_failure_rolls_back_project_creation(env, monkeypatch):
    client, factory = env

    def boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.services.dmrv.projects.record_audit", boom)
    response = create(client)
    assert response.status_code == 500
    assert (count(factory, DmrvProject), count(factory, ProjectMember), count(factory, DmrvIdempotencyKey)) == (0, 0, 0)


def test_dmrv_project_create_permission_is_seeded_for_admin_not_viewer():
    from scripts.seed_rbac import ADMIN_PERMISSIONS, VIEWER_PERMISSIONS

    assert "dmrv.project.create" in ADMIN_PERMISSIONS
    assert "dmrv.project.create" not in VIEWER_PERMISSIONS


def test_patch_with_null_name_is_422_not_500(env):
    client, factory = env
    project_id = create(client).json()["id"]
    response = call(client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"', json={"name": None})
    assert response.status_code == 422
    with factory() as db:
        assert db.get(DmrvProject, project_id).revision == 1


def _seed_snapshot(factory, project_id, snapshot_id="snap-evidence-1", outputs=None):
    with factory() as db:
        db.add(DmrvSnapshot(
            id=snapshot_id,
            project_id=project_id,
            job_id=None,
            kind="field_summary",
            inputs={"monitoring_period_id": "period-1"},
            outputs=outputs if outputs is not None else {"plot_count": 2, "visit_count": 3},
            snapshot_hash=(snapshot_id.encode().hex() * 32)[:64],
            created_by="admin:1",
        ))
        db.commit()


def test_evidence_package_is_reproducible_and_explicitly_incomplete(env, monkeypatch, tmp_path):
    client, factory = env
    monkeypatch.setenv("DMRV_EVIDENCE_DIR", str(tmp_path))
    project_id = create(client).json()["id"]
    _seed_snapshot(factory, project_id)
    url = f"/dmrv/projects/{project_id}/evidence-packages"

    first = call(client, "POST", url, key="evidence-1", json={"snapshot_id": "snap-evidence-1"})
    replay = call(client, "POST", url, key="evidence-1", json={"snapshot_id": "snap-evidence-1"})
    assert (first.status_code, replay.status_code) == (201, 200)
    body = first.json()
    assert body["id"] == replay.json()["id"]
    assert body["readiness"] == "incomplete"
    assert len(body["missing_components"]) == 8
    assert body["manifest"]["verification_status"] == "not_verified"
    assert body["manifest"]["issuance_status"] == "not_issued"

    verify = call(client, "GET", f"{url}/{body['id']}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
    assert verify.json()["checked_entries"] == len(body["manifest"]["entries"])
    download = call(client, "GET", f"{url}/{body['id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert download.content[:2] == b"PK"
    assert count(factory, DmrvEvidencePackage) == 1
    with factory() as db:
        event = db.execute(
            select(DmrvAuditEvent).where(DmrvAuditEvent.action == "evidence.package.create")
        ).scalar_one()
        assert event.snapshot_hash == db.get(DmrvEvidencePackage, body["id"]).manifest["snapshot"]["sha256"]


def test_evidence_package_integrity_failure_blocks_download(env, monkeypatch, tmp_path):
    client, factory = env
    monkeypatch.setenv("DMRV_EVIDENCE_DIR", str(tmp_path))
    project_id = create(client).json()["id"]
    _seed_snapshot(factory, project_id)
    created = call(
        client, "POST", f"/dmrv/projects/{project_id}/evidence-packages", key="evidence-2",
        json={"snapshot_id": "snap-evidence-1"},
    ).json()
    with factory() as db:
        row = db.get(DmrvEvidencePackage, created["id"])
        package_path = tmp_path / row.storage_key
    package_path.write_bytes(package_path.read_bytes() + b"tampered")

    verify = call(client, "GET", f"/dmrv/projects/{project_id}/evidence-packages/{created['id']}/verify")
    download = call(client, "GET", f"/dmrv/projects/{project_id}/evidence-packages/{created['id']}/download")
    assert verify.json()["valid"] is False
    assert verify.json()["code"] == "package_hash_mismatch"
    assert download.status_code == 409


def test_evidence_package_access_is_project_scoped(env, monkeypatch, tmp_path):
    client, factory = env
    monkeypatch.setenv("DMRV_EVIDENCE_DIR", str(tmp_path))
    project_id = create(client).json()["id"]
    _seed_snapshot(factory, project_id)
    created = call(
        client, "POST", f"/dmrv/projects/{project_id}/evidence-packages", key="evidence-3",
        json={"snapshot_id": "snap-evidence-1"},
    ).json()
    foreign = call(
        client, "GET", f"/dmrv/projects/{project_id}/evidence-packages/{created['id']}/download",
        viewer="admin:2",
    )
    assert foreign.status_code == 404


def test_evidence_audit_failure_rolls_back_package_and_file(env, monkeypatch, tmp_path):
    from app.services.dmrv import evidence as evidence_service

    client, factory = env
    monkeypatch.setenv("DMRV_EVIDENCE_DIR", str(tmp_path))
    project_id = create(client).json()["id"]
    _seed_snapshot(factory, project_id)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(evidence_service, "record_audit", fail_audit)
    response = call(
        client, "POST", f"/dmrv/projects/{project_id}/evidence-packages", key="evidence-fail",
        json={"snapshot_id": "snap-evidence-1"},
    )
    assert response.status_code == 500
    assert count(factory, DmrvEvidencePackage) == 0
    assert list(tmp_path.iterdir()) == []


def _seed_complete_evidence(client, factory, project_id, tmp_path, monkeypatch):
    required = (
        "methodology_mapping_and_approval",
        "monitoring_run_scene_and_model_provenance",
        "baseline_or_documented_not_applicable_decision",
        "accounting_ledger_and_input_snapshot",
        "uncertainty_and_leakage_assessment",
        "field_qa_qc_and_data_chain_of_custody",
        "spatial_artifacts_and_quality_report",
        "reproducible_report_and_distribution_licences",
    )
    outputs = {"evidence_components": {
        name: {"status": "provided", "artifact": {"fixture": "review-test"}}
        for name in required
    }}
    _seed_snapshot(factory, project_id, "snap-ready-1", outputs)
    monkeypatch.setenv("DMRV_EVIDENCE_DIR", str(tmp_path))
    response = call(
        client,
        method="POST",
        url=f"/dmrv/projects/{project_id}/evidence-packages",
        key="ready-evidence-key",
        json={"snapshot_id": "snap-ready-1"},
    )
    assert response.status_code == 201
    assert response.json()["readiness"] == "ready"
    return response.json()


def test_submission_stays_blocked_until_evidence_package_is_complete(env, monkeypatch, tmp_path):
    client, factory = env
    project_id = create(client).json()["id"]
    _seed_snapshot(factory, project_id)
    monkeypatch.setenv("DMRV_EVIDENCE_DIR", str(tmp_path))
    package = call(
        client, "POST", f"/dmrv/projects/{project_id}/evidence-packages", key="draft-evidence-key",
        json={"snapshot_id": "snap-evidence-1"},
    ).json()
    url = f"/dmrv/projects/{project_id}/submissions"
    draft = call(
        client, "POST", url, key="submission-key", json={"evidence_package_id": package["id"]},
    )
    replay = call(
        client, "POST", url, key="submission-key", json={"evidence_package_id": package["id"]},
    )
    assert draft.status_code == 201
    assert draft.json()["status"] == "draft"
    assert replay.status_code == 200
    assert replay.json()["id"] == draft.json()["id"]
    response = call(
        client, "POST", f"{url}/{draft.json()['id']}/submit",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "evidence_incomplete"
    assert count(factory, DmrvSubmission) == 1
    with factory() as db:
        assert db.get(DmrvSubmission, draft.json()["id"]).status == "draft"


def test_submission_review_enforces_independence_and_audits_state_changes(env, monkeypatch, tmp_path):
    client, factory = env
    project_id = create(client).json()["id"]
    package = _seed_complete_evidence(client, factory, project_id, tmp_path, monkeypatch)
    added = call(
        client, "POST", f"/dmrv/projects/{project_id}/members",
        json={"principal_type": "user", "username": "field-user", "role": "reviewer"},
    )
    assert added.status_code == 201
    submissions_url = f"/dmrv/projects/{project_id}/submissions"
    draft = call(
        client, "POST", submissions_url, key="submission-ready-key",
        json={"evidence_package_id": package["id"]},
    ).json()
    submission_id = draft["id"]
    submitted = call(client, "POST", f"{submissions_url}/{submission_id}/submit")
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted"

    review_url = f"{submissions_url}/{submission_id}/review/start"
    author_start = call(client, "POST", review_url, viewer="admin:1")
    assert author_start.status_code == 403
    started = call(client, "POST", review_url, viewer="user:1")
    assert started.status_code == 200 and started.json()["status"] == "under_review"
    replayed_start = call(client, "POST", review_url, viewer="user:1")
    assert replayed_start.status_code == 200

    findings_url = f"{submissions_url}/{submission_id}/findings"
    finding = call(
        client, "POST", findings_url, viewer="user:1", key="finding-idempotency-key",
        json={"code": "DATA_NOTE", "message": "Tambahkan catatan provenance untuk fixture."},
    )
    assert finding.status_code == 201 and finding.json()["status"] == "open"
    duplicate_finding = call(
        client, "POST", findings_url, viewer="user:1", key="finding-idempotency-key",
        json={"code": "DATA_NOTE", "message": "Tambahkan catatan provenance untuk fixture."},
    )
    assert duplicate_finding.status_code == 200
    assert duplicate_finding.json()["id"] == finding.json()["id"]
    finding_id = finding.json()["id"]
    respond_url = f"{findings_url}/{finding_id}/respond"
    response = call(
        client, "POST", respond_url, viewer="admin:1",
        json={"response": "Catatan provenance dilampirkan.", "evidence_references": [package["id"]]},
    )
    assert response.status_code == 200 and response.json()["status"] == "responded"
    resolve_url = f"{findings_url}/{finding_id}/resolve"
    author_resolve = call(client, "POST", resolve_url, viewer="admin:1")
    assert author_resolve.status_code == 403
    resolved = call(client, "POST", resolve_url, viewer="user:1")
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"

    decision = call(
        client, "POST", f"{submissions_url}/{submission_id}/decision", viewer="user:1",
        json={"decision": "approved", "reason": "Fixture test review completed."},
    )
    assert decision.status_code == 200 and decision.json()["status"] == "approved"
    snapshot = call(client, "GET", f"/dmrv/projects/{project_id}/snapshots/snap-ready-1")
    assert snapshot.status_code == 200
    assert snapshot.json()["mrv_review_status"]["status"] == "approved"
    assert snapshot.json()["mrv_review_status"]["reviewer_key"] == "user:1"
    assert snapshot.json()["mrv_review_status"]["decided_at"]
    assert snapshot.json()["mrv_review_status"]["decision_reason"] == "Fixture test review completed."
    with factory() as db:
        actions = db.execute(select(DmrvAuditEvent.action).where(
            DmrvAuditEvent.project_id == project_id,
        ).order_by(DmrvAuditEvent.id)).scalars().all()
    assert actions.count("submission.review.start") == 1
    assert "review.finding.create" in actions
    assert "review.finding.respond" in actions
    assert "review.finding.resolve" in actions
    assert "submission.approved" in actions


def test_submission_review_audit_failure_rolls_back_transition(env, monkeypatch, tmp_path):
    from app.services.dmrv import submissions as submission_service

    client, factory = env
    project_id = create(client).json()["id"]
    package = _seed_complete_evidence(client, factory, project_id, tmp_path, monkeypatch)
    member = call(
        client, "POST", f"/dmrv/projects/{project_id}/members",
        json={"principal_type": "user", "username": "field-user", "role": "reviewer"},
    )
    assert member.status_code == 201
    draft = call(
        client, "POST", f"/dmrv/projects/{project_id}/submissions", key="audit-draft-key",
        json={"evidence_package_id": package["id"]},
    ).json()
    submission_id = draft["id"]
    assert call(client, "POST", f"/dmrv/projects/{project_id}/submissions/{submission_id}/submit").status_code == 200

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(submission_service, "record_audit", fail_audit)
    response = call(
        client, "POST", f"/dmrv/projects/{project_id}/submissions/{submission_id}/review/start", viewer="user:1",
    )
    assert response.status_code == 500
    with factory() as db:
        assert db.get(DmrvSubmission, submission_id).status == "submitted"
        assert db.get(DmrvSubmission, submission_id).reviewer_key is None
