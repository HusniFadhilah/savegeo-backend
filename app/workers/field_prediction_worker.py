"""Lease-backed worker for long-running GEE field prediction extraction."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import socket
import threading
import uuid

from sqlalchemy import select

from app.db.models.carbon_calibration import CarbonCalibrationRun
from app.db.session import SessionLocal

log = logging.getLogger("savegeo.field-prediction-worker")
JOB_METHOD = "gee_field_prediction"
BACKOFF_MAX_SECONDS = 300


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def claim(session_factory, worker_id: str, lease_seconds: int) -> str | None:
    now = _now()
    with session_factory() as db:
        row = db.execute(
            select(CarbonCalibrationRun)
            .where(CarbonCalibrationRun.method == JOB_METHOD, CarbonCalibrationRun.status == "queued",
                   CarbonCalibrationRun.run_after <= now)
            .order_by(CarbonCalibrationRun.run_after, CarbonCalibrationRun.created_at, CarbonCalibrationRun.id)
            .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            db.rollback()
            return None
        row.status, row.worker_id = "running", worker_id
        row.attempts += 1
        row.heartbeat_at = now
        row.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
        row.progress = max(row.progress, 5)
        db.commit()
        return row.id


def heartbeat(session_factory, run_id: str, worker_id: str, lease_seconds: int) -> bool:
    now = _now()
    with session_factory() as db:
        row = db.execute(select(CarbonCalibrationRun).where(
            CarbonCalibrationRun.id == run_id, CarbonCalibrationRun.status == "running",
            CarbonCalibrationRun.worker_id == worker_id,
            CarbonCalibrationRun.lease_expires_at > now,
        ).with_for_update()).scalar_one_or_none()
        if row is None:
            db.rollback()
            return False
        row.heartbeat_at = now
        row.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
        db.commit()
        return True


def reconcile_expired(session_factory) -> int:
    now = _now()
    count = 0
    with session_factory() as db:
        rows = db.execute(select(CarbonCalibrationRun).where(
            CarbonCalibrationRun.method == JOB_METHOD, CarbonCalibrationRun.status == "running",
            CarbonCalibrationRun.lease_expires_at < now,
        ).with_for_update(skip_locked=True)).scalars().all()
        for row in rows:
            count += 1
            row.worker_id = None
            row.lease_expires_at = None
            if row.attempts >= row.max_attempts:
                row.status, row.progress = "failed", 100
                row.error = {"code": "field_prediction_lease_expired",
                             "message": "Worker ekstraksi berhenti merespons dan batas percobaan habis."}
                row.completed_at = now
            else:
                row.status = "queued"
                row.run_after = now + dt.timedelta(seconds=min(5 * (2 ** row.attempts), BACKOFF_MAX_SECONDS))
        db.commit()
    return count


def _fail(session_factory, run_id: str, worker_id: str, exc: Exception) -> None:
    from app.services.gee_common import AnalysisError
    import requests

    now = _now()
    message = str(exc).lower()
    transient_ee = type(exc).__module__.startswith("ee.") and any(token in message for token in (
        "429", "500", "502", "503", "504", "timeout", "timed out", "temporarily unavailable",
        "internal error", "deadline exceeded",
    ))
    retryable = isinstance(exc, (requests.RequestException, TimeoutError, ConnectionError)) or transient_ee or (
        isinstance(exc, AnalysisError) and exc.status_code in {408, 429, 502, 503, 504}
    )
    with session_factory() as db:
        row = db.execute(select(CarbonCalibrationRun).where(
            CarbonCalibrationRun.id == run_id, CarbonCalibrationRun.status == "running",
            CarbonCalibrationRun.worker_id == worker_id,
        ).with_for_update()).scalar_one_or_none()
        if row is None:
            db.rollback()
            return
        row.worker_id, row.lease_expires_at = None, None
        # Background jobs may receive provider URLs, tokens, or internal paths.
        # Keep those details in the protected server log, never in job status.
        safe = "Ekstraksi Earth Engine gagal. Periksa layanan dan coba lagi."
        row.error = {"code": "field_prediction_failed", "message": safe, "retryable": retryable}
        if retryable and row.attempts < row.max_attempts:
            row.status = "queued"
            row.run_after = now + dt.timedelta(seconds=min(5 * (2 ** row.attempts), BACKOFF_MAX_SECONDS))
        else:
            row.status, row.progress, row.completed_at = "failed", 100, now
        db.commit()


def run_one(session_factory, worker_id: str, lease_seconds: int = 180) -> bool:
    run_id = claim(session_factory, worker_id, lease_seconds)
    if run_id is None:
        return False
    stop, lease_lost = threading.Event(), threading.Event()

    def beat() -> None:
        while not stop.wait(max(lease_seconds / 3, 5)):
            try:
                if not heartbeat(session_factory, run_id, worker_id, lease_seconds):
                    lease_lost.set()
                    return
            except Exception:  # noqa: BLE001 - a transient DB failure is retried on the next heartbeat
                log.exception("field prediction heartbeat failed run_id=%s", run_id)

    thread = threading.Thread(target=beat, name=f"field-hb-{run_id[:8]}", daemon=True)
    thread.start()
    try:
        from app.services import gee_service
        with session_factory() as db:
            if not gee_service.initialize_ee(db):
                from app.services.gee_common import AnalysisError
                raise AnalysisError("Earth Engine belum siap; ekstraksi dijadwalkan ulang.", 503)
        if lease_lost.is_set():
            return True
        with session_factory() as db:
            from app.services.field_prediction_service import run_field_prediction
            run_field_prediction(db, run_id, worker_id)
    except Exception as exc:  # noqa: BLE001 - persist a bounded public error, keep worker alive
        log.exception("field prediction failed run_id=%s", run_id)
        _fail(session_factory, run_id, worker_id, exc)
    finally:
        stop.set()
        thread.join(timeout=5)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="durable GEE field prediction worker")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=int, default=180)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("worker %s started", worker_id)
    while not stop.is_set():
        try:
            reconcile_expired(SessionLocal)
            worked = run_one(SessionLocal, worker_id, args.lease_seconds)
        except Exception:  # noqa: BLE001 - database/provider outages must not terminate the service
            log.exception("field prediction worker loop error")
            worked = False
        if args.once:
            return 0
        if not worked:
            stop.wait(args.poll_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
