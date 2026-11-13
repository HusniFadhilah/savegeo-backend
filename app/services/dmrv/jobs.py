"""PostgreSQL-backed durable job queue for dMRV.

Workers claim a job with ``SELECT ... FOR UPDATE SKIP LOCKED`` and hold a lease
that a heartbeat keeps extending. Every worker write is *fenced*: it only
applies while ``status == running`` and ``lease_owner == worker_id``. A worker
that lost its lease (crash, stall, network partition) therefore cannot
complete, fail or otherwise modify the job; whatever it staged in its session is
rolled back. ``reap_expired`` returns abandoned jobs to the queue with backoff.

All functions take an explicit ``now`` so behaviour is deterministic in tests.
``enqueue`` only flushes (the API commits it together with its audit event); all
worker-side functions commit.
"""
from __future__ import annotations

import copy
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.dmrv_job import DmrvJob

BACKOFF_BASE_SECONDS = 5
BACKOFF_CAP_SECONDS = 300
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class HeartbeatResult:
    ok: bool
    cancel_requested: bool = False


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(dt.UTC)


def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_BASE_SECONDS * 2 ** attempts, BACKOFF_CAP_SECONDS)


def enqueue(
    db: Session, *, project_id: str, kind: str, payload: dict, created_by: str,
    idempotency_key: str | None = None, request_hash: str | None = None, max_attempts: int = 3,
    job_id: str | None = None, now: dt.datetime | None = None,
) -> DmrvJob:
    moment = _now(now)
    job = DmrvJob(
        id=job_id or str(uuid.uuid4()), project_id=project_id, kind=kind, status="queued", payload=payload,
        progress={}, attempts=0, max_attempts=max_attempts, cancel_requested=False, run_after=moment,
        created_by=created_by, idempotency_key=idempotency_key, request_hash=request_hash, created_at=moment,
    )
    db.add(job)
    db.flush()
    return job


def claim_next(db: Session, worker_id: str, lease_seconds: int = 60, now: dt.datetime | None = None) -> DmrvJob | None:
    moment = _now(now)
    job = db.execute(
        select(DmrvJob)
        .where(DmrvJob.status == "queued", DmrvJob.run_after <= moment)
        .order_by(DmrvJob.created_at, DmrvJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.lease_owner = worker_id
    job.lease_expires_at = moment + dt.timedelta(seconds=lease_seconds)
    job.heartbeat_at = moment
    job.attempts += 1
    job.started_at = job.started_at or moment
    db.commit()
    return job


def _owned(db: Session, job_id: str, worker_id: str) -> DmrvJob | None:
    return db.execute(
        select(DmrvJob)
        .where(DmrvJob.id == job_id, DmrvJob.status == "running", DmrvJob.lease_owner == worker_id)
        .with_for_update()
    ).scalar_one_or_none()


def _close_last_stage(job: DmrvJob, moment: dt.datetime) -> None:
    progress = copy.deepcopy(job.progress or {})
    stages = progress.get("stages", [])
    if stages and not stages[-1].get("finished_at"):
        stages[-1]["finished_at"] = moment.isoformat()
        progress["stages"] = stages
        job.progress = progress


def heartbeat(db: Session, job_id: str, worker_id: str, lease_seconds: int = 60, now: dt.datetime | None = None) -> HeartbeatResult:
    moment = _now(now)
    job = _owned(db, job_id, worker_id)
    if job is None:
        db.rollback()
        return HeartbeatResult(False)
    job.heartbeat_at = moment
    job.lease_expires_at = moment + dt.timedelta(seconds=lease_seconds)
    cancel = bool(job.cancel_requested)
    db.commit()
    return HeartbeatResult(True, cancel)


def set_stage(db: Session, job_id: str, worker_id: str, stage: str, now: dt.datetime | None = None) -> bool:
    moment = _now(now)
    job = _owned(db, job_id, worker_id)
    if job is None:
        db.rollback()
        return False
    _close_last_stage(job, moment)
    progress = copy.deepcopy(job.progress or {})
    progress.setdefault("stages", []).append({"name": stage, "started_at": moment.isoformat()})
    progress["stage"] = stage
    job.progress = progress
    db.commit()
    return True


def complete(db: Session, job_id: str, worker_id: str, result: dict[str, Any], now: dt.datetime | None = None) -> bool:
    """Commit the caller's staged writes together with the success transition, or discard them all."""
    moment = _now(now)
    job = _owned(db, job_id, worker_id)
    if job is None:
        db.rollback()
        return False
    if job.cancel_requested:
        db.rollback()
        return False
    _close_last_stage(job, moment)
    job.status, job.result, job.error = "succeeded", result, None
    job.finished_at, job.lease_owner, job.lease_expires_at = moment, None, None
    _settle_analysis(db, job, True, moment)
    db.commit()
    return True


def fail(
    db: Session, job_id: str, worker_id: str, error: dict[str, Any], retryable: bool, now: dt.datetime | None = None,
) -> str:
    moment = _now(now)
    db.rollback()  # discard anything the failed handler staged
    job = _owned(db, job_id, worker_id)
    if job is None:
        db.rollback()
        return "lost"
    _close_last_stage(job, moment)
    job.error = {**error, "retryable": bool(retryable)}
    job.lease_owner, job.lease_expires_at = None, None
    if retryable and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = moment + dt.timedelta(seconds=backoff_seconds(job.attempts))
    else:
        job.status, job.finished_at = "failed", moment
        _settle_analysis(db, job, False, moment)
    status = job.status
    db.commit()
    return status


def request_cancel(db: Session, job_id: str, now: dt.datetime | None = None) -> str:
    moment = _now(now)
    job = db.execute(select(DmrvJob).where(DmrvJob.id == job_id).with_for_update()).scalar_one_or_none()
    if job is None:
        db.rollback()
        return "missing"
    if job.status == "queued":
        job.status, job.finished_at, job.cancel_requested = "cancelled", moment, True
        _settle_analysis(db, job, False, moment)
        outcome = "cancelled"
    elif job.status == "running":
        job.cancel_requested = True
        outcome = "cancelling"
    else:
        outcome = "finished"
    db.commit()
    return outcome


def mark_cancelled(db: Session, job_id: str, worker_id: str, now: dt.datetime | None = None) -> bool:
    moment = _now(now)
    db.rollback()
    job = _owned(db, job_id, worker_id)
    if job is None:
        db.rollback()
        return False
    _close_last_stage(job, moment)
    job.status, job.finished_at = "cancelled", moment
    job.lease_owner, job.lease_expires_at = None, None
    _settle_analysis(db, job, False, moment)
    db.commit()
    return True


def reap_expired(db: Session, now: dt.datetime | None = None) -> int:
    """Reconcile jobs whose worker vanished: requeue with backoff, fail at max attempts, honour cancellation."""
    moment = _now(now)
    expired = db.execute(
        select(DmrvJob)
        .where(DmrvJob.status == "running", DmrvJob.lease_expires_at < moment)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    for job in expired:
        job.lease_owner, job.lease_expires_at = None, None
        if job.cancel_requested:
            job.status, job.finished_at = "cancelled", moment
        elif job.attempts >= job.max_attempts:
            job.status, job.finished_at = "failed", moment
            job.error = {"code": "lease_expired", "message": "Worker berhenti merespons dan batas percobaan habis.", "retryable": False}
        else:
            job.status = "queued"
            job.run_after = moment + dt.timedelta(seconds=backoff_seconds(job.attempts))
        if job.status in {"failed", "cancelled"}:
            _settle_analysis(db, job, False, moment)
    db.commit()
    return len(expired)


def _settle_analysis(db: Session, job: DmrvJob, succeeded: bool, now: dt.datetime) -> None:
    if job.kind.startswith("analysis."):
        from app.services.workspace_usage import settle
        settle(db, job.id, succeeded, now)
