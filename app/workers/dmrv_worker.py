"""dMRV job worker: ``python -m app.workers.dmrv_worker``.

Claims jobs from the PostgreSQL queue, runs the registered handler under a
lease kept alive by a heartbeat thread, and reaps jobs abandoned by dead workers.
Several workers can run at once; each job attempt is claimed by exactly one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import socket
import threading
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.services.dmrv import jobs
from app.services.dmrv.job_handlers import (
    Handler,
    JobCancelled,
    JobContext,
    JobFailed,
    JobLost,
    RetryableJobError,
    get_handlers,
)

logger = logging.getLogger("dmrv.worker")
GENERIC_ERROR = "Pekerjaan gagal. Coba lagi atau hubungi administrator."


def run_one(
    session_factory: Callable[[], Session], worker_id: str, lease_seconds: int = 60,
    handlers: dict[str, Handler] | None = None, now: dt.datetime | None = None,
    heartbeat_interval: float | None = None,
) -> str | None:
    """Claim and run one job. Returns its resulting state, or None when the queue had nothing runnable.

    ``now`` pins the clock (tests); ``heartbeat_interval`` starts the background heartbeat thread.
    """
    handlers = handlers if handlers is not None else get_handlers()
    clock = (lambda: now) if now is not None else (lambda: dt.datetime.now(dt.UTC))
    work, control = session_factory(), session_factory()
    stop, cancel_event, lost_event = threading.Event(), threading.Event(), threading.Event()
    thread: threading.Thread | None = None
    try:
        job = jobs.claim_next(work, worker_id, lease_seconds, clock())
        if job is None:
            return None
        handler = handlers.get(job.kind)
        if handler is None:
            return jobs.fail(work, job.id, worker_id, {"code": "unknown_job_kind", "message": f"Jenis pekerjaan tidak dikenal: {job.kind}"}, False, clock())

        if heartbeat_interval:
            def beat() -> None:
                while not stop.wait(heartbeat_interval):
                    try:
                        with session_factory() as session:
                            result = jobs.heartbeat(session, job.id, worker_id, lease_seconds, clock())
                    except Exception:  # noqa: BLE001 - a transient DB error must not kill the thread
                        logger.exception("heartbeat failed job=%s", job.id)
                        continue
                    if not result.ok:
                        lost_event.set()
                        return
                    if result.cancel_requested:
                        cancel_event.set()

            thread = threading.Thread(target=beat, name=f"hb-{job.id[:8]}", daemon=True)
            thread.start()

        ctx = JobContext(
            db=work, control=control, job_id=job.id, worker_id=worker_id, project_id=job.project_id, payload=job.payload,
            created_by=job.created_by, lease_seconds=lease_seconds, clock=clock, cancel_event=cancel_event, lost_event=lost_event,
        )
        try:
            result = handler.run(ctx)
            return "succeeded" if jobs.complete(work, job.id, worker_id, result, clock()) else "lost"
        except JobCancelled:
            work.rollback()
            return "cancelled" if jobs.mark_cancelled(work, job.id, worker_id, clock()) else "lost"
        except JobLost:
            work.rollback()
            return "lost"
        except RetryableJobError as exc:
            return jobs.fail(work, job.id, worker_id, {"code": exc.code, "message": exc.message}, True, clock())
        except JobFailed as exc:
            return jobs.fail(work, job.id, worker_id, {"code": exc.code, "message": exc.message}, False, clock())
        except Exception:  # noqa: BLE001 - never store internals; log them server-side only
            logger.exception("job %s (%s) failed", job.id, job.kind)
            return jobs.fail(work, job.id, worker_id, {"code": "internal_error", "message": GENERIC_ERROR}, False, clock())
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
        work.close()
        control.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dMRV job worker")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    logger.info("worker %s started (lease %ss, poll %ss)", worker_id, args.lease_seconds, args.poll_interval)

    while not stopping.is_set():
        try:
            with SessionLocal() as session:
                reaped = jobs.reap_expired(session)
            if reaped:
                logger.warning("reaped %s abandoned job(s)", reaped)
            outcome = run_one(SessionLocal, worker_id, args.lease_seconds, heartbeat_interval=max(args.lease_seconds / 3, 1.0))
        except Exception:  # noqa: BLE001 - keep the loop alive across transient database errors
            logger.exception("worker loop error")
            outcome = None
        if args.once:
            return 0
        if outcome is None:
            stopping.wait(args.poll_interval)
    logger.info("worker %s stopped", worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
