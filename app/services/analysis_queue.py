"""PostgreSQL queue with claim fencing, recovery and owner-bound idempotency."""
from __future__ import annotations
import hashlib
import json
import re
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, text

from app.core.config import get_settings
from app.db.models.analysis_job import AnalysisJob

FINAL = {"succeeded", "failed", "cancelled"}


def enabled():
    settings = get_settings()
    hosted = settings.app_env in {"production", "staging"}
    if hosted and settings.analysis_queue_backend == "inline":
        raise RuntimeError("Hosted deployments require the durable analysis queue")
    return hosted or settings.analysis_queue_backend == "postgres"


def prepare(db, kind, payload, owner, cache_key=None, idempotency_key=None):
    if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise HTTPException(422, "Invalid Idempotency-Key")
    digest = hashlib.sha256(json.dumps([kind, payload], sort_keys=True, allow_nan=False).encode()).hexdigest()
    # Serialize admissions only, not execution. The count is global across
    # all web processes, so a per-process semaphore cannot multiply capacity.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(729401701)"))
    if idempotency_key:
        existing = db.query(AnalysisJob).filter_by(owner=owner, idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_hash != digest:
                raise HTTPException(409, "Idempotency-Key already refers to different input")
            return existing, False
    if db.query(AnalysisJob).filter(AnalysisJob.status.in_(["preparing", "queued", "running"])).count() >= 24:
        raise HTTPException(429, "Analysis queue is full", headers={"Retry-After": "30"})
    now = time.time()
    job_id = str(uuid.uuid4())
    job = AnalysisJob(id=job_id, owner=owner, kind=kind, payload=payload, cache_key=cache_key,
        status="preparing", idempotency_key=idempotency_key, request_hash=digest, attempts=0,
        cancel_requested=False, created_at=now, updated_at=now,
        document={"job_id": job_id, "owner": owner, "type": kind, "status": "queued"})
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, True


def activate(db, job_id):
    db.query(AnalysisJob).filter_by(id=job_id, status="preparing").update({"status": "queued"})
    db.commit()


def reap(db, now=None):
    now = time.time() if now is None else now
    rows = db.query(AnalysisJob).filter(or_(
        (AnalysisJob.status == "running") & (AnalysisJob.lease_expires_at < now),
        (AnalysisJob.status == "preparing") & (AnalysisJob.created_at < now - 120),
    )).with_for_update(skip_locked=True).limit(100).all()
    for row in rows:
        state = "cancelled" if row.cancel_requested else "queued" if row.attempts < 3 and row.status == "running" else "failed"
        row.status, row.lease_owner, row.lease_expires_at, row.updated_at = state, None, None, now
        row.document = {**row.document, "status": state,
            **({"error": "Pekerjaan terhenti dan tidak dapat dipulihkan.", "status_code": 503} if state == "failed" else {})}
    db.commit()
    return len(rows)


def claim(db, token, now=None, lease=60):
    now = time.time() if now is None else now
    row = db.query(AnalysisJob).filter_by(status="queued", cancel_requested=False).order_by(
        AnalysisJob.created_at, AnalysisJob.id).with_for_update(skip_locked=True).first()
    if row is None:
        db.rollback()
        return None
    row.status, row.lease_owner, row.lease_expires_at = "running", token, now + lease
    row.attempts, row.heartbeat_at, row.updated_at = row.attempts + 1, now, now
    row.document = {**row.document, "status": "running"}
    db.commit()
    db.refresh(row)
    return row


def heartbeat(db, job_id, token, now=None, lease=60):
    now = time.time() if now is None else now
    ok = db.query(AnalysisJob).filter(AnalysisJob.id == job_id, AnalysisJob.status == "running",
        AnalysisJob.lease_owner == token, AnalysisJob.lease_expires_at >= now,
        AnalysisJob.cancel_requested.is_(False)).update(
            {"heartbeat_at": now, "lease_expires_at": now + lease}, synchronize_session=False)
    db.commit()
    return bool(ok)


def write_document(db, job_id, document, token=None):
    row = db.get(AnalysisJob, job_id, with_for_update=True)
    if row is None:
        return False
    if token and (row.status != "running" or row.lease_owner != token
            or row.lease_expires_at < time.time() or row.cancel_requested):
        raise RuntimeError("Analysis lease is no longer owned")
    if not token and row.status == "running":
        raise RuntimeError("Analysis lease is no longer owned")
    if row.status in FINAL:
        raise RuntimeError("Analysis job is already final")
    row.document = {**document, "owner": row.owner}
    row.status, row.updated_at = document["status"], time.time()
    db.commit()
    return True


def cancel(db, job_id, owner):
    row = db.query(AnalysisJob).filter_by(id=job_id, owner=owner).with_for_update().first()
    if row is None:
        raise HTTPException(404, "Job not found")
    if row.status in FINAL:
        raise HTTPException(409, "Job is no longer cancellable")
    row.cancel_requested = True
    if row.status != "running":
        row.status = "cancelled"
        row.document = {**row.document, "status": "cancelled"}
    db.commit()
    return {"job_id": row.id, "status": "cancel_requested" if row.status == "running" else row.status}
