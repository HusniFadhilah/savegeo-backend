import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models.dmrv_audit import DmrvAuditEvent
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.services.dmrv import field_data, job_handlers, jobs
from app.services.dmrv.authz import Principal
from app.services.dmrv.field_import import ImportOptions
from app.workers.dmrv_worker import run_one
from tests.test_dmrv_field_data import GOOD, csv_bytes

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
OWNER = Principal("admin", 1)
OPTIONS = ImportOptions(height_unit="cm", plot_area_m2=400.0, area_basis="assumed")
TABLES = [
    DmrvProject.__table__, ProjectMember.__table__, DmrvAuditEvent.__table__, ImportBatch.__table__,
    SurveyPlot.__table__, SurveyVisit.__table__, TreeMeasurement.__table__, BoundaryVersion.__table__,
    DmrvJob.__table__, DmrvSnapshot.__table__,
]


def at(seconds):
    return NOW + dt.timedelta(seconds=seconds)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(engine, tables=TABLES)
    maker = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with maker() as db:
        db.add(DmrvProject(id="p-1", name="A"))
        db.add(ProjectMember(project_id="p-1", principal_type="admin", principal_id=1, role="project_owner", added_by="admin:1"))
        db.commit()
    yield maker
    engine.dispose()


def import_trees(factory, *rows, name="trees.csv"):
    with factory() as db:
        return field_data.commit_import(db, OWNER, "p-1", name, csv_bytes(*rows), None, OPTIONS, "atomic", "req-1")


def enqueue(factory, kind="field_summary", payload=None, max_attempts=3):
    with factory() as db:
        job = jobs.enqueue(db, project_id="p-1", kind=kind, payload=payload or {}, created_by="admin:1",
                           max_attempts=max_attempts, now=NOW)
        db.commit()
        return job.id


def row(factory, model, key):
    with factory() as db:
        return db.get(model, key)


def count(factory, model):
    with factory() as db:
        return db.execute(select(func.count()).select_from(model)).scalar_one()


def test_empty_queue_returns_none(factory):
    assert run_one(factory, "w-1", now=NOW) is None


def test_field_summary_creates_an_audited_snapshot_with_locked_inputs(factory):
    batch = import_trees(factory, *GOOD)
    job_id = enqueue(factory)
    assert run_one(factory, "w-1", now=at(1)) == "succeeded"

    job = row(factory, DmrvJob, job_id)
    assert job.status == "succeeded" and job.result["reused"] is False
    assert [s["name"] for s in job.progress["stages"]] == ["collect", "compute", "snapshot"]
    with factory() as db:
        snap = db.execute(select(DmrvSnapshot)).scalar_one()
        audit = db.execute(select(DmrvAuditEvent).where(DmrvAuditEvent.action == "snapshot.create")).scalar_one()
    assert snap.id == job.result["snapshot_id"] and snap.snapshot_hash == job.result["snapshot_hash"]
    assert snap.kind == "field_summary" and snap.job_id == job_id and snap.created_by == "admin:1"
    assert snap.inputs["algorithm"] == "field_summary_v1" and snap.inputs["boundary"] is None
    assert snap.inputs["import_batches"] == [{"id": batch["batch_id"], "sha256": snap.inputs["import_batches"][0]["sha256"]}]
    assert snap.inputs["carbon_fraction"] == 0.47
    plot = snap.outputs["plots"][0]
    assert (plot["plot_code"], plot["area_basis"], plot["area_m2"]) == ("1", "assumed", 400.0)
    assert plot["visits"][0]["density"]["carbon_mg_c_per_ha"] == pytest.approx((205.4 + 137.8) / 1000 / 0.04 * 0.47, rel=1e-6)
    assert snap.outputs["counts"] == {"plots_total": 1, "plots_with_density": 1, "plots_blocked": 0}
    assert (audit.actor_type, audit.actor_id, audit.snapshot_hash, audit.project_id) == ("admin", 1, snap.snapshot_hash, "p-1")


def test_unchanged_data_reuses_the_snapshot_and_new_data_creates_a_new_one(factory):
    import_trees(factory, *GOOD)
    first = enqueue(factory)
    run_one(factory, "w-1", now=at(1))
    second = enqueue(factory)
    run_one(factory, "w-1", now=at(2))
    first_result, second_result = row(factory, DmrvJob, first).result, row(factory, DmrvJob, second).result
    assert second_result["reused"] is True and second_result["snapshot_id"] == first_result["snapshot_id"]
    assert count(factory, DmrvSnapshot) == 1

    import_trees(factory, "1,2022,1,Asem,60,19.1,700,150.0,-7.0527,110.4396", name="older.csv")
    third = enqueue(factory)
    run_one(factory, "w-1", now=at(3))
    third_result = row(factory, DmrvJob, third).result
    assert third_result["reused"] is False and third_result["snapshot_hash"] != first_result["snapshot_hash"]
    assert count(factory, DmrvSnapshot) == 2
    old = row(factory, DmrvSnapshot, first_result["snapshot_id"])
    assert old.snapshot_hash == first_result["snapshot_hash"] and len(old.outputs["plots"][0]["visits"]) == 1


def test_project_without_field_data_fails_with_a_clear_code(factory):
    job_id = enqueue(factory)
    assert run_one(factory, "w-1", now=at(1)) == "failed"
    job = row(factory, DmrvJob, job_id)
    assert job.error["code"] == "no_field_data" and job.error["retryable"] is False
    assert count(factory, DmrvSnapshot) == 0


def test_unknown_kind_fails_without_retry(factory):
    job_id = enqueue(factory, kind="nope")
    assert run_one(factory, "w-1", now=at(1)) == "failed"
    assert row(factory, DmrvJob, job_id).error["code"] == "unknown_job_kind"


def test_retryable_error_is_retried_with_backoff_then_fails(factory):
    def flaky(ctx):
        raise job_handlers.RetryableJobError("upstream_unavailable", "Layanan eksternal tidak tersedia.")

    handlers = {"flaky": job_handlers.Handler(validate=lambda p: p, run=flaky)}
    job_id = enqueue(factory, kind="flaky", max_attempts=2)
    assert run_one(factory, "w-1", handlers=handlers, now=at(0)) == "queued"
    assert run_one(factory, "w-1", handlers=handlers, now=at(5)) is None  # backoff (10 s) not over
    assert run_one(factory, "w-1", handlers=handlers, now=at(20)) == "failed"
    job = row(factory, DmrvJob, job_id)
    assert (job.status, job.attempts, job.error["code"]) == ("failed", 2, "upstream_unavailable")


def test_unexpected_exception_is_stored_without_internals(factory):
    def boom(ctx):
        raise RuntimeError("secret internal detail: password=hunter2")

    handlers = {"boom": job_handlers.Handler(validate=lambda p: p, run=boom)}
    job_id = enqueue(factory, kind="boom")
    assert run_one(factory, "w-1", handlers=handlers, now=at(1)) == "failed"
    error = row(factory, DmrvJob, job_id).error
    assert error["code"] == "internal_error"
    assert "hunter2" not in str(error) and "Traceback" not in str(error)


def test_handler_writes_are_rolled_back_when_it_fails_after_staging_them(factory):
    def stage_then_fail(ctx):
        ctx.db.add(DmrvSnapshot(id="s-x", project_id="p-1", kind="field_summary", inputs={}, outputs={},
                                snapshot_hash="x" * 64, created_by="admin:1"))
        ctx.db.flush()
        raise RuntimeError("late failure")

    handlers = {"k": job_handlers.Handler(validate=lambda p: p, run=stage_then_fail)}
    enqueue(factory, kind="k")
    assert run_one(factory, "w-1", handlers=handlers, now=at(1)) == "failed"
    assert count(factory, DmrvSnapshot) == 0


def test_cancel_requested_during_a_run_ends_cancelled_at_the_next_checkpoint(factory):
    def cancellable(ctx):
        with factory() as other:
            assert jobs.request_cancel(other, ctx.job_id, now=at(2)) == "cancelling"
        ctx.checkpoint()
        return {"never": "reached"}

    handlers = {"c": job_handlers.Handler(validate=lambda p: p, run=cancellable)}
    job_id = enqueue(factory, kind="c")
    assert run_one(factory, "w-1", handlers=handlers, now=at(1)) == "cancelled"
    job = row(factory, DmrvJob, job_id)
    assert job.status == "cancelled" and job.result is None


def test_a_worker_that_lost_its_lease_cannot_finish_or_leave_data_behind(factory):
    def stalls(ctx):
        # while this worker is "stalled", the reaper takes the job and worker B claims it
        with factory() as other:
            assert jobs.reap_expired(other, now=at(1000)) == 1
            assert jobs.claim_next(other, "w-B", lease_seconds=60, now=at(1100)).id == ctx.job_id
        ctx.db.add(DmrvSnapshot(id="s-late", project_id="p-1", kind="field_summary", inputs={}, outputs={},
                                snapshot_hash="l" * 64, created_by="admin:1"))
        return {"late": True}

    handlers = {"s": job_handlers.Handler(validate=lambda p: p, run=stalls)}
    job_id = enqueue(factory, kind="s")
    assert run_one(factory, "w-A", lease_seconds=10, handlers=handlers, now=at(0)) == "lost"
    job = row(factory, DmrvJob, job_id)
    assert (job.status, job.lease_owner, job.result) == ("running", "w-B", None)
    assert count(factory, DmrvSnapshot) == 0


def test_test_handlers_exist_only_when_enabled(monkeypatch):
    monkeypatch.delenv("DMRV_TEST_HANDLERS", raising=False)
    assert set(job_handlers.get_handlers()) == {"field_summary"}
    monkeypatch.setenv("DMRV_TEST_HANDLERS", "1")
    assert {"field_summary", "_test_sleep"} <= set(job_handlers.get_handlers())
