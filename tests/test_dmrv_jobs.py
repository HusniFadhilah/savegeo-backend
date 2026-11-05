import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models.dmrv_job import DmrvJob
from app.db.models.dmrv_project import DmrvProject
from app.services.dmrv import jobs

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return NOW + dt.timedelta(seconds=seconds)


def utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=dt.UTC)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    DmrvProject.__table__.create(engine)
    DmrvJob.__table__.create(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add(DmrvProject(id="p-1", name="A"))
        session.commit()
        yield session
    engine.dispose()


def make(db: Session, job_id="j-1", created=0, **over) -> DmrvJob:
    values = {"project_id": "p-1", "kind": "field_summary", "payload": {}, "created_by": "admin:1",
              "idempotency_key": None, "request_hash": None}
    values.update(over)
    job = jobs.enqueue(db, **{k: values[k] for k in ("project_id", "kind", "payload", "created_by", "idempotency_key", "request_hash")},
                       max_attempts=values.get("max_attempts", 3), job_id=job_id, now=at(created))
    return job


def reload(db: Session, job_id: str) -> DmrvJob:
    db.expire_all()
    return db.get(DmrvJob, job_id)


def test_claim_returns_oldest_runnable_job_and_sets_the_lease(db):
    make(db, "late", created=10)
    make(db, "early", created=1)
    claimed = jobs.claim_next(db, "w-1", lease_seconds=60, now=at(20))
    assert claimed.id == "early" and claimed.status == "running"
    row = reload(db, "early")
    assert (row.lease_owner, row.attempts) == ("w-1", 1)
    assert utc(row.lease_expires_at) == at(80) and utc(row.started_at) == at(20)


def test_empty_queue_and_future_run_after_are_not_claimed(db):
    assert jobs.claim_next(db, "w-1", now=at(0)) is None
    make(db, "later")
    job = reload(db, "later")
    job.run_after = at(100)
    db.commit()
    assert jobs.claim_next(db, "w-1", now=at(50)) is None
    assert jobs.claim_next(db, "w-1", now=at(100)).id == "later"


def test_a_running_job_is_not_claimed_twice(db):
    make(db, "j-1")
    assert jobs.claim_next(db, "w-1", now=at(1)).id == "j-1"
    assert jobs.claim_next(db, "w-2", now=at(2)) is None


def test_expired_lease_is_requeued_with_backoff_then_reclaimed(db):
    make(db, "j-1")
    jobs.claim_next(db, "w-1", lease_seconds=60, now=at(0))
    assert jobs.reap_expired(db, now=at(30)) == 0  # still leased
    assert jobs.reap_expired(db, now=at(61)) == 1
    row = reload(db, "j-1")
    assert row.status == "queued" and row.lease_owner is None and utc(row.run_after) == at(61 + 10)
    assert jobs.claim_next(db, "w-2", now=at(65)) is None  # backoff not over
    again = jobs.claim_next(db, "w-2", now=at(72))
    assert again.id == "j-1" and reload(db, "j-1").attempts == 2


def test_third_lost_lease_fails_the_job(db):
    make(db, "j-1", max_attempts=2)
    jobs.claim_next(db, "w-1", lease_seconds=10, now=at(0))
    jobs.reap_expired(db, now=at(11))
    jobs.claim_next(db, "w-2", lease_seconds=10, now=at(100))
    assert jobs.reap_expired(db, now=at(111)) == 1
    row = reload(db, "j-1")
    assert row.status == "failed" and row.error["code"] == "lease_expired" and row.error["retryable"] is False
    assert utc(row.finished_at) == at(111)


def test_a_stale_worker_cannot_touch_the_job(db):
    make(db, "j-1")
    jobs.claim_next(db, "w-A", lease_seconds=10, now=at(0))
    jobs.reap_expired(db, now=at(11))
    jobs.claim_next(db, "w-B", lease_seconds=60, now=at(30))
    assert jobs.heartbeat(db, "j-1", "w-A", now=at(31)).ok is False
    assert jobs.set_stage(db, "j-1", "w-A", "collect", now=at(31)) is False
    assert jobs.complete(db, "j-1", "w-A", {"x": 1}, now=at(32)) is False
    assert jobs.fail(db, "j-1", "w-A", {"code": "x", "message": "m"}, retryable=False, now=at(32)) == "lost"
    row = reload(db, "j-1")
    assert (row.status, row.lease_owner, row.result, row.error) == ("running", "w-B", None, None)
    assert jobs.complete(db, "j-1", "w-B", {"ok": True}, now=at(40)) is True
    assert reload(db, "j-1").status == "succeeded"


def test_heartbeat_extends_the_lease_and_reports_cancellation(db):
    make(db, "j-1")
    jobs.claim_next(db, "w-1", lease_seconds=60, now=at(0))
    beat = jobs.heartbeat(db, "j-1", "w-1", lease_seconds=60, now=at(40))
    assert beat.ok and beat.cancel_requested is False
    assert utc(reload(db, "j-1").lease_expires_at) == at(100)
    assert jobs.reap_expired(db, now=at(70)) == 0
    jobs.request_cancel(db, "j-1", now=at(41))
    assert jobs.heartbeat(db, "j-1", "w-1", now=at(42)).cancel_requested is True


def test_stages_are_recorded_in_order_and_closed_on_completion(db):
    make(db, "j-1")
    jobs.claim_next(db, "w-1", now=at(0))
    jobs.set_stage(db, "j-1", "w-1", "collect", now=at(1))
    jobs.set_stage(db, "j-1", "w-1", "compute", now=at(2))
    jobs.complete(db, "j-1", "w-1", {"ok": True}, now=at(3))
    progress = reload(db, "j-1").progress
    assert progress["stage"] == "compute"
    assert [s["name"] for s in progress["stages"]] == ["collect", "compute"]
    assert all(s.get("finished_at") for s in progress["stages"])


def test_retryable_failure_backs_off_then_fails_at_max_attempts(db):
    make(db, "j-1", max_attempts=2)
    jobs.claim_next(db, "w-1", now=at(0))
    assert jobs.fail(db, "j-1", "w-1", {"code": "upstream", "message": "m"}, retryable=True, now=at(5)) == "queued"
    row = reload(db, "j-1")
    assert row.status == "queued" and utc(row.run_after) == at(15) and row.error["code"] == "upstream"
    jobs.claim_next(db, "w-1", now=at(20))
    assert jobs.fail(db, "j-1", "w-1", {"code": "upstream", "message": "m"}, retryable=True, now=at(25)) == "failed"
    assert reload(db, "j-1").status == "failed"


def test_non_retryable_failure_is_final(db):
    make(db, "j-1")
    jobs.claim_next(db, "w-1", now=at(0))
    assert jobs.fail(db, "j-1", "w-1", {"code": "bad", "message": "m"}, retryable=False, now=at(1)) == "failed"


def test_cancel_matrix(db):
    make(db, "queued")
    assert jobs.request_cancel(db, "queued", now=at(1)) == "cancelled"
    assert reload(db, "queued").status == "cancelled" and jobs.claim_next(db, "w", now=at(2)) is None

    make(db, "running")
    jobs.claim_next(db, "w-1", now=at(3))
    assert jobs.request_cancel(db, "running", now=at(4)) == "cancelling"
    row = reload(db, "running")
    assert row.status == "running" and row.cancel_requested is True
    assert jobs.mark_cancelled(db, "running", "w-1", now=at(5)) is True
    assert reload(db, "running").status == "cancelled"

    make(db, "done")
    jobs.claim_next(db, "w-1", now=at(6))
    jobs.complete(db, "done", "w-1", {}, now=at(7))
    assert jobs.request_cancel(db, "done", now=at(8)) == "finished"
    assert reload(db, "done").status == "succeeded"


def test_cancel_requested_job_with_a_dead_worker_ends_cancelled(db):
    make(db, "j-1")
    jobs.claim_next(db, "w-1", lease_seconds=10, now=at(0))
    jobs.request_cancel(db, "j-1", now=at(1))
    jobs.reap_expired(db, now=at(11))
    assert reload(db, "j-1").status == "cancelled"
