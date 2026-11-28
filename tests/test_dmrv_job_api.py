import csv
import io

from sqlalchemy import select

from app.db.models.dmrv_audit import DmrvAuditEvent
from app.db.models.dmrv_job import DmrvJob
from app.db.models.dmrv_job import DmrvSnapshot
from app.workers.dmrv_worker import run_one
from tests.test_dmrv_field_api import upload
from tests.test_dmrv_projects import call, create, env as dmrv_env  # noqa: F401 (pytest fixture)


def test_job_api_is_idempotent_scoped_and_audited(dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    project_id = create(client).json()["id"]
    other_project_id = create(client, key="k-2").json()["id"]
    path = f"/dmrv/projects/{project_id}/jobs"
    payload = {"kind": "field_summary"}

    created = call(client, "POST", path, key="job-1", json=payload)
    assert created.status_code == 202
    job_id = created.json()["id"]
    replay = call(client, "POST", path, key="job-1", json=payload)
    assert replay.status_code == 200 and replay.json()["id"] == job_id
    reused_elsewhere = call(
        client, "POST", f"/dmrv/projects/{other_project_id}/jobs", key="job-1", json=payload,
    )
    assert reused_elsewhere.status_code == 409
    assert call(client, "GET", f"{path}/{job_id}").json()["status"] == "queued"
    assert call(client, "GET", f"/dmrv/projects/{project_id}/jobs").json()[0]["id"] == job_id
    assert call(client, "GET", f"/dmrv/projects/{project_id}/jobs/{job_id}", viewer="admin:2").status_code == 404

    cancelled = call(client, "POST", f"{path}/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["cancel_requested"] is True
    with factory() as db:
        actions = db.execute(select(DmrvAuditEvent.action)).scalars().all()
        assert actions.count("job.create") == 1 and actions.count("job.cancel") == 1
        assert db.get(DmrvJob, job_id).status == "cancelled"


def test_field_summary_runs_through_api_worker_and_snapshot_read_api(dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    project_id = create(client).json()["id"]
    imported = upload(client, project_id, mode="atomic", key="field-1")
    assert imported.status_code == 201
    created = call(client, "POST", f"/dmrv/projects/{project_id}/jobs", key="job-success", json={"kind": "field_summary"})
    assert created.status_code == 202
    job_id = created.json()["id"]

    assert run_one(factory, "test-worker") == "succeeded"

    finished = call(client, "GET", f"/dmrv/projects/{project_id}/jobs/{job_id}")
    assert finished.status_code == 200
    result = finished.json()["result"]
    assert finished.json()["status"] == "succeeded"
    assert result["snapshot_hash"]
    snapshot = call(client, "GET", f"/dmrv/projects/{project_id}/snapshots/{result['snapshot_id']}")
    assert snapshot.status_code == 200
    assert snapshot.json()["snapshot_hash"] == result["snapshot_hash"]
    assert snapshot.json()["outputs"]["counts"]["plots_total"] == 1
    with factory() as db:
        assert db.execute(select(DmrvSnapshot).where(DmrvSnapshot.project_id == project_id)).scalars().first() is not None


def test_carbon_snapshot_csv_is_audited_and_project_scoped(dmrv_env):  # noqa: F811
    client, factory = dmrv_env
    owner_project = create(client, viewer="admin:1", key="csv-project-owner")
    other_project = create(client, viewer="admin:2", key="csv-project-other")
    owner_id, other_id = owner_project.json()["id"], other_project.json()["id"]
    snapshot_id = "carbon-csv-snapshot"
    with factory() as db:
        db.add(DmrvSnapshot(
            id=snapshot_id,
            project_id=owner_id,
            job_id=None,
            kind="carbon",
            inputs={"context": {"project_id": owner_id, "period": {"id": "period-2026"}}},
            outputs={
                "carbon_estimated": {"unit": "Mg C/ha", "statistics": {"mean": 12.5, "std_dev": 2.0}},
                "area_info": {"total_carbon_tons": 25.0},
                "carbon_mrv": {
                    "estimate": {"status": "model_estimate", "pool": "AGB", "source_dataset": "WCMC", "source_year": 2026},
                    "calibration": {"status": "not_requested"},
                    "validation": {"status": "not_validated", "validation_type": "none"},
                    "review": {"status": "not_reviewed"},
                    "accounting": {"status": "not_calculated"},
                    "registry": {"status": "not_connected"},
                },
            },
            snapshot_hash="c" * 64,
            created_by="admin:1",
        ))
        db.commit()

    csv_response = call(
        client, "GET", f"/dmrv/projects/{owner_id}/snapshots/{snapshot_id}/carbon-statistics.csv", viewer="admin:1",
    )
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]
    row = next(csv.DictReader(io.StringIO(csv_response.text)))
    assert row["project_id"] == owner_id
    assert row["monitoring_period_id"] == "period-2026"
    assert row["result_version"] == "c" * 64
    assert row["validation_type"] == "none"
    assert row["accounting_status"] == "not_calculated"

    cross_project = call(
        client, "GET", f"/dmrv/projects/{other_id}/snapshots/{snapshot_id}/carbon-statistics.csv", viewer="admin:2",
    )
    assert cross_project.status_code == 404
    with factory() as db:
        actions = db.execute(select(DmrvAuditEvent.action)).scalars().all()
        assert actions.count("snapshot.csv.export") == 1
