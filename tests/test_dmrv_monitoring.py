from sqlalchemy import select

from app.db.models.dmrv_audit import DmrvAuditEvent, DmrvIdempotencyKey
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.services.dmrv import monitoring
from app.workers.dmrv_worker import run_one
from tests.test_dmrv_field_api import upload
from tests.test_dmrv_projects import SQUARE, call, create, env as dmrv_env  # noqa: F401


def _project_with_boundary(client):
    project_id = create(client).json()["id"]
    boundary = call(client, "POST", f"/dmrv/projects/{project_id}/boundaries", json={"geojson": SQUARE, "reason": "fixture"})
    assert boundary.status_code == 201
    return project_id


def _create_period(client, project_id, *, name="2025", start="2025-01-01", end="2025-12-31", version=1, key="period-1", viewer="admin:1"):
    return call(
        client, "POST", f"/dmrv/projects/{project_id}/monitoring-periods", viewer=viewer, key=key,
        json={"name": name, "start_date": start, "end_date": end, "boundary_version": version},
    )


def test_period_create_idempotency_and_audit_are_atomic(dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    project_id = _project_with_boundary(client)
    first = _create_period(client, project_id)
    replay = _create_period(client, project_id)
    assert first.status_code == 201 and first.headers["ETag"] == '"1"'
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    assert first.json()["boundary_version"] == 1 and first.json()["status"] == "planned"
    with factory() as db:
        assert len(db.execute(select(DmrvMonitoringPeriod)).scalars().all()) == 1
        assert db.execute(select(DmrvIdempotencyKey).where(DmrvIdempotencyKey.key == "period-1")).scalar_one()
        assert db.execute(select(DmrvAuditEvent.action).where(DmrvAuditEvent.action == "monitoring_period.create")).scalar_one()


def test_period_requires_project_boundary_and_prevents_overlap(dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    project_id = create(client).json()["id"]
    missing_boundary = _create_period(client, project_id)
    assert missing_boundary.status_code == 422 and missing_boundary.json()["code"] == "boundary_version_not_found"
    project_id = _project_with_boundary(client)
    assert _create_period(client, project_id).status_code == 201
    overlap = _create_period(client, project_id, name="overlap", start="2025-06-01", end="2026-01-01", key="overlap")
    assert overlap.status_code == 409 and overlap.json()["code"] == "monitoring_period_overlap"
    reversed_dates = _create_period(client, project_id, name="reversed", start="2026-01-01", end="2025-01-01", key="reversed")
    assert reversed_dates.status_code == 422
    with factory() as db:
        assert len(db.execute(select(DmrvMonitoringPeriod)).scalars().all()) == 1


def test_period_lifecycle_is_versioned_and_completed_period_is_locked(dmrv_env):  # noqa: F811
    client, _ = dmrv_env
    project_id = _project_with_boundary(client)
    period = _create_period(client, project_id).json()
    path = f"/dmrv/projects/{project_id}/monitoring-periods/{period['id']}"
    started = call(client, "PATCH", path, if_match='"1"', json={"status": "in_progress"})
    assert started.status_code == 200 and started.json()["revision"] == 2
    stale = call(client, "PATCH", path, if_match='"1"', json={"name": "lost update"})
    assert stale.status_code == 412
    forbidden_edit = call(client, "PATCH", path, if_match='"2"', json={"name": "changed"})
    assert forbidden_edit.status_code == 409 and forbidden_edit.json()["code"] == "period_in_progress"
    completed = call(client, "PATCH", path, if_match='"2"', json={"status": "completed"})
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    locked = call(client, "PATCH", path, if_match='"3"', json={"name": "changed"})
    assert locked.status_code == 409 and locked.json()["code"] == "period_completed"


def test_period_membership_and_role_scope(dmrv_env):  # noqa: F811
    client, _ = dmrv_env
    project_id = _project_with_boundary(client)
    period = _create_period(client, project_id).json()
    period_path = f"/dmrv/projects/{project_id}/monitoring-periods/{period['id']}"
    assert call(client, "GET", period_path, viewer="admin:2").status_code == 404
    assert call(client, "GET", f"/dmrv/projects/{project_id}/monitoring-periods", viewer="user:1").status_code == 404


def test_audit_failure_rolls_back_monitoring_period(monkeypatch, dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    project_id = _project_with_boundary(client)
    monkeypatch.setattr(monitoring, "record_audit", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit down")))
    failed = _create_period(client, project_id)
    assert failed.status_code == 500
    with factory() as db:
        assert not db.execute(select(DmrvMonitoringPeriod)).scalars().all()
        assert not db.execute(select(DmrvIdempotencyKey).where(DmrvIdempotencyKey.key == "period-1")).scalars().all()


def test_period_scoped_summary_uses_frozen_boundary_and_filters_visits(dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    project_id = _project_with_boundary(client)
    imported = upload(client, project_id, key="field-2024")
    assert imported.status_code == 201
    period = _create_period(client, project_id, name="2024", start="2024-01-01", end="2024-12-31").json()
    path = f"/dmrv/projects/{project_id}/monitoring-periods/{period['id']}"
    assert call(client, "PATCH", path, if_match='"1"', json={"status": "in_progress"}).status_code == 200
    created = call(client, "POST", f"/dmrv/projects/{project_id}/jobs", key="summary-2024", json={
        "kind": "field_summary", "monitoring_period_id": period["id"],
    })
    assert created.status_code == 202
    job_id = created.json()["id"]
    assert run_one(factory, "monitoring-test-worker") == "succeeded"
    with factory() as db:
        job = db.get(DmrvJob, job_id)
        snapshot = db.execute(select(DmrvSnapshot).where(DmrvSnapshot.project_id == project_id)).scalar_one()
        assert job.status == "succeeded"
        assert snapshot.inputs["monitoring_period"]["id"] == period["id"]
        assert snapshot.inputs["boundary"]["checksum"] == period["boundary_checksum"]
        assert snapshot.outputs["plots"][0]["visits"][0]["visit_date"] == "2024-01-01"
