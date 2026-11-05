"""Job handlers for the dMRV worker.

A handler receives a ``JobContext``. Its database writes happen on ``ctx.db`` and
stay uncommitted until the runner calls ``jobs.complete`` under a valid lease, so
a stalled or cancelled worker leaves nothing behind. Progress (``ctx.stage``) and
liveness (``ctx.checkpoint``) use a separate control session, so they never commit
the handler's pending writes: call them *before* staging writes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_job import DmrvSnapshot
from app.services.dmrv import jobs
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal
from app.services.dmrv.field_data import plot_visits
from app.services.dmrv.field_metrics import DEFAULT_CARBON_FRACTION

FIELD_SUMMARY_ALGORITHM = "field_summary_v1"


class JobCancelled(Exception):
    """Cancellation was requested; the runner finishes the job as cancelled."""


class JobLost(Exception):
    """This worker no longer owns the job's lease; it must stop without writing."""


class JobFailed(Exception):
    """A permanent, user-explainable failure (stored as the job error)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class RetryableJobError(JobFailed):
    """A transient failure (upstream outage, timeout); the job is retried with backoff."""


@dataclass
class JobContext:
    db: Session
    control: Session
    job_id: str
    worker_id: str
    project_id: str
    payload: dict
    created_by: str
    lease_seconds: int
    clock: Callable[[], dt.datetime]
    cancel_event: threading.Event
    lost_event: threading.Event

    @property
    def principal(self) -> Principal:
        kind, ident = self.created_by.split(":")
        return Principal(kind, int(ident))  # type: ignore[arg-type]

    def stage(self, name: str) -> None:
        if self.lost_event.is_set() or not jobs.set_stage(self.control, self.job_id, self.worker_id, name, self.clock()):
            raise JobLost(self.job_id)

    def checkpoint(self) -> None:
        """Extend the lease and raise if the job was cancelled or the lease was lost."""
        beat = jobs.heartbeat(self.control, self.job_id, self.worker_id, self.lease_seconds, self.clock())
        if self.lost_event.is_set() or not beat.ok:
            raise JobLost(self.job_id)
        if beat.cancel_requested or self.cancel_event.is_set():
            raise JobCancelled(self.job_id)


@dataclass(frozen=True)
class Handler:
    validate: Callable[[dict], dict]
    run: Callable[[JobContext], dict]


def _validate_empty(payload: dict) -> dict:
    if payload:
        raise ValueError("payload field_summary harus kosong")
    return {}


def _canonical_hash(kind: str, inputs: dict, outputs: dict) -> str:
    document = {"kind": kind, "algorithm": FIELD_SUMMARY_ALGORITHM, "inputs": inputs, "outputs": outputs}
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_field_summary(ctx: JobContext) -> dict:
    db = ctx.db
    ctx.stage("collect")
    plots = db.execute(
        select(SurveyPlot).where(SurveyPlot.project_id == ctx.project_id).order_by(SurveyPlot.plot_code)
    ).scalars().all()
    if not plots:
        raise JobFailed("no_field_data", "Proyek belum memiliki data lapangan untuk diringkas.")
    boundary = db.execute(
        select(BoundaryVersion).where(BoundaryVersion.project_id == ctx.project_id).order_by(BoundaryVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    batch_ids = {
        batch_id for (batch_id,) in db.execute(
            select(TreeMeasurement.batch_id).distinct()
            .join(SurveyVisit, SurveyVisit.id == TreeMeasurement.visit_id)
            .join(SurveyPlot, SurveyPlot.id == SurveyVisit.plot_id)
            .where(SurveyPlot.project_id == ctx.project_id)
        ).all() if batch_id
    }
    batches = [
        {"id": b.id, "sha256": b.sha256}
        for b in db.execute(select(ImportBatch).where(ImportBatch.id.in_(sorted(batch_ids))).order_by(ImportBatch.id)).scalars()
    ] if batch_ids else []
    ctx.checkpoint()

    ctx.stage("compute")
    out_plots = [
        {"plot_code": p.plot_code, "area_m2": p.area_m2, "area_basis": p.area_basis, "visits": plot_visits(db, p)}
        for p in plots
    ]
    with_density = sum(1 for p in out_plots if any(v["density"]["carbon_mg_c_per_ha"] is not None for v in p["visits"]))
    counts = {"plots_total": len(out_plots), "plots_with_density": with_density, "plots_blocked": len(out_plots) - with_density}
    inputs = {
        "algorithm": FIELD_SUMMARY_ALGORITHM,
        "boundary": {"version": boundary.version, "checksum": boundary.checksum} if boundary else None,
        "import_batches": batches,
        "carbon_fraction": float(DEFAULT_CARBON_FRACTION),
    }
    outputs = {"plots": out_plots, "counts": counts}
    snapshot_hash = _canonical_hash("field_summary", inputs, outputs)
    ctx.checkpoint()

    ctx.stage("snapshot")  # control-session write first; from here on only ctx.db writes are staged
    existing = db.execute(
        select(DmrvSnapshot).where(DmrvSnapshot.project_id == ctx.project_id, DmrvSnapshot.snapshot_hash == snapshot_hash)
    ).scalar_one_or_none()
    if existing is not None:
        return {"snapshot_id": existing.id, "snapshot_hash": snapshot_hash, "reused": True}
    snapshot = DmrvSnapshot(
        id=str(uuid.uuid4()), project_id=ctx.project_id, job_id=ctx.job_id, kind="field_summary",
        inputs=inputs, outputs=outputs, snapshot_hash=snapshot_hash, created_by=ctx.created_by,
    )
    db.add(snapshot)
    db.flush()
    record_audit(
        db, principal=ctx.principal, action="snapshot.create", resource_type="snapshot", resource_id=snapshot.id,
        project_id=ctx.project_id, after={"kind": "field_summary", "job_id": ctx.job_id, **counts}, snapshot_hash=snapshot_hash,
    )
    return {"snapshot_id": snapshot.id, "snapshot_hash": snapshot_hash, "reused": False}


def _test_sleep(ctx: JobContext) -> dict:
    deadline = time.monotonic() + float(ctx.payload.get("seconds", 1))
    ctx.stage("sleeping")
    while time.monotonic() < deadline:
        time.sleep(0.2)
        ctx.checkpoint()
    return {"slept": ctx.payload.get("seconds", 1)}


def _test_fail(ctx: JobContext) -> dict:
    raise RetryableJobError("test_retryable", "Kegagalan uji yang dapat diulang.")


def get_handlers() -> dict[str, Handler]:
    handlers = {"field_summary": Handler(validate=_validate_empty, run=run_field_summary)}
    if os.environ.get("DMRV_TEST_HANDLERS") == "1":
        handlers["_test_sleep"] = Handler(validate=lambda p: p, run=_test_sleep)
        handlers["_test_fail"] = Handler(validate=lambda p: p, run=_test_fail)
    return handlers

