"""Project context, transactional quota and durable execution contracts.

Provider calls are stubbed here. PostgreSQL cases use a unique throwaway schema;
these tests do not claim scientific validation or live GEE coverage.
"""
import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from tests.test_organizations import call, workspace
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.workspace_analysis import WorkspaceQuota, WorkspaceUsageEvent
from app.db.models.organization import OrganizationMember
from app.schemas.workspace_analysis import ProjectAnalysisCreate
from app.services import workspace_analysis as service
from app.services.dmrv import analysis_handler, jobs
from app.services.dmrv.errors import DmrvError
from app.workers.dmrv_worker import run_one

pytest_plugins = ["tests.test_organizations"]


def setup_project(client, factory):
    org = workspace(client, "admin:1")
    result = call(client, "POST", "/dmrv/projects", "admin:1", json={"name": "Carbon pilot", "organization_id": org})
    assert result.status_code == 201, result.text
    project = result.json()["id"]
    boundary, period = str(uuid.uuid4()), str(uuid.uuid4())
    polygon = {"type": "Polygon", "coordinates": [[[110, -7], [110.01, -7], [110.01, -7.01], [110, -7.01], [110, -7]]]}
    with factory() as db:
        db.add(BoundaryVersion(id=boundary, project_id=project, version=1, original_geojson=polygon,
                               validated_geojson=polygon, crs="EPSG:4326", area_ha=122, area_method="geodesic",
                               checksum="a" * 64, created_by="admin:1", reason="survey"))
        db.flush()
        db.add(DmrvMonitoringPeriod(id=period, project_id=project, boundary_version_id=boundary, name="2026",
                                    start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 12, 31),
                                    status="in_progress", created_by="admin:1"))
        db.commit()
    return org, project, period


def submit(client, project, period, key=None, **changes):
    payload = {"kind": "carbon", "monitoring_period_id": period, **changes}
    return call(client, "POST", f"/dmrv/projects/{project}/analysis-runs", "admin:1", key=key or uuid.uuid4().hex, json=payload)


def test_server_context_replay_foreign_resources_and_cancel_release(env):
    client, factory, _ = env
    org, project, period = setup_project(client, factory)
    result = submit(client, project, period, "submit-once")
    assert result.status_code == 202, result.text
    job_id = result.json()["id"]
    assert submit(client, project, period, "submit-once").json()["id"] == job_id
    assert submit(client, project, period, "submit-once", parameters={"scale": 100}).status_code == 409
    assert submit(client, project, uuid.uuid4().hex).status_code == 404
    assert submit(client, project, period, parameters={"aoi": {}}).status_code == 422
    assert call(client, "GET", f"/dmrv/projects/{project}/analysis-runs", "user:1").status_code == 404
    rows = call(client, "GET", f"/dmrv/projects/{project}/analysis-runs", "admin:1").json()
    assert len(rows) == 1 and rows[0]["context"]["boundary"]["area_ha"] == 122
    assert rows[0]["context"]["parameters"]["year"] == 2026
    usage = call(client, "GET", f"/organizations/{org}/usage", "admin:1").json()
    assert (usage["reserved"], usage["used"]) == (1, 0)
    cancelled = call(client, "POST", f"/dmrv/projects/{project}/analysis-runs/{job_id}/cancel", "admin:1")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert call(client, "POST", f"/dmrv/projects/{project}/analysis-runs/{job_id}/cancel", "admin:1").status_code == 200
    with factory() as db:
        quota = db.scalar(select(WorkspaceQuota))
        assert (quota.reserved, quota.used) == (0, 0)
        assert len(list(db.scalars(select(WorkspaceUsageEvent)))) == 2


def test_worker_snapshot_and_meter_finalize_once(env, monkeypatch):
    client, factory, _ = env
    org, project, period = setup_project(client, factory)
    result = submit(client, project, period)
    assert result.status_code == 202, result.text
    monkeypatch.setattr("app.services.gee_service.initialize_ee", lambda db: True)
    seen = []

    def execute(db, kind, parameters):
        seen.append(parameters)
        return {"mean_carbon": 31.5, "total_carbon": 3843, "unit": "Mg C", "model_version": "fixture-only"}

    monkeypatch.setattr(analysis_handler, "execute", execute)
    assert run_one(factory, "test-worker") == "succeeded"
    assert run_one(factory, "second-worker") is None
    assert len(seen) == 1 and seen[0]["aoi"]["geojson"]["type"] == "Polygon"
    with factory() as db:
        job = db.get(DmrvJob, result.json()["id"])
        snapshot = db.get(DmrvSnapshot, job.result["snapshot_id"])
        assert snapshot.inputs["context"]["period"]["id"] == period
        assert snapshot.outputs["mean_carbon"] == 31.5
        quota = db.scalar(select(WorkspaceQuota))
        assert (quota.used, quota.reserved) == (1, 0)
        assert not jobs.complete(db, job.id, "test-worker", {})
        assert len(list(db.scalars(select(WorkspaceUsageEvent)))) == 2


def test_worker_failure_releases_and_retry_keeps_reservation(env, monkeypatch):
    client, factory, _ = env
    _, project, period = setup_project(client, factory)
    job_id = submit(client, project, period).json()["id"]
    with factory() as db:
        jobs.claim_next(db, "worker")
        assert jobs.fail(db, job_id, "worker", {"code": "upstream"}, True) == "queued"
        quota = db.scalar(select(WorkspaceQuota))
        assert (quota.reserved, quota.used) == (1, 0)
        job = db.get(DmrvJob, job_id)
        job.run_after = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        db.commit()
        jobs.claim_next(db, "worker2")
        assert jobs.fail(db, job_id, "worker2", {"code": "invalid_input"}, False) == "failed"
        db.refresh(quota)
        assert (quota.reserved, quota.used) == (0, 0)


def test_concurrent_reservations_do_not_exceed_quota(env):
    client, factory, dialect = env
    if dialect != "postgresql":
        pytest.skip("Concurrency requires PostgreSQL row locks")
    org, project, period = setup_project(client, factory)
    call(client, "GET", f"/organizations/{org}/usage", "admin:1")
    with factory() as db:
        db.scalar(select(WorkspaceQuota)).run_limit = 1
        db.commit()
    barrier = threading.Barrier(2)

    def launch():
        with factory() as db:
            viewer = db.get(AdminUser, 1)
            barrier.wait(timeout=10)
            try:
                service.submit(db, viewer, project, ProjectAnalysisCreate(kind="carbon", monitoring_period_id=period), uuid.uuid4().hex)
                return "accepted"
            except DmrvError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(launch) for _ in range(2)]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(outcomes) == ["accepted", "quota_exhausted"]
    with factory() as db:
        quota = db.scalar(select(WorkspaceQuota))
        assert quota.reserved == 1 and quota.used == 0
        assert len(list(db.scalars(select(DmrvJob)))) == 1


def test_revoked_membership_prevents_provider_execution(env, monkeypatch):
    client, factory, _ = env
    org, project, period = setup_project(client, factory)
    job_id = submit(client, project, period).json()["id"]
    with factory() as db:
        member = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == org))
        member.is_active = False
        db.commit()
    def unexpected(*args):
        pytest.fail("Provider must not be called after access revocation")
    monkeypatch.setattr("app.services.gee_service.initialize_ee", unexpected)
    assert run_one(factory, "worker") == "failed"
    assert call(client, "GET", f"/dmrv/projects/{project}/jobs/{job_id}", "admin:1").status_code == 404
    with factory() as db:
        assert db.scalar(select(WorkspaceQuota)).reserved == 0
        assert len(list(db.scalars(select(DmrvSnapshot)))) == 0


def test_cancel_during_inference_cannot_publish_snapshot(env, monkeypatch):
    client, factory, _ = env
    _, project, period = setup_project(client, factory)
    job_id = submit(client, project, period).json()["id"]
    monkeypatch.setattr("app.services.gee_service.initialize_ee", lambda db: True)
    def execute(db, kind, parameters):
        with factory() as control:
            assert jobs.request_cancel(control, job_id) == "cancelling"
        return {"value": 42}
    monkeypatch.setattr(analysis_handler, "execute", execute)
    assert run_one(factory, "worker") == "cancelled"
    with factory() as db:
        assert db.scalar(select(WorkspaceQuota)).reserved == 0
        assert len(list(db.scalars(select(DmrvSnapshot)))) == 0
