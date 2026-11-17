"""Lease-backed queue worker with disposable attempts and hard cancellation."""
from __future__ import annotations
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from app.db.models.analysis_job import AnalysisJob
from app.db.session import SessionLocal
from app.services import analysis_queue as queue

log = logging.getLogger("savegeo.analysis-worker")


def stop_process(process):
    if process.poll() is None:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=10)


def run_one(stopping, *, timeout=900):
    token = uuid.uuid4().hex
    with SessionLocal() as db:
        queue.reap(db)
        job = queue.claim(db, token)
        if job is None:
            return False
        job_id = job.id
    env = {**os.environ, "SAVEGEO_ANALYSIS_LEASE": token, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    process = subprocess.Popen([sys.executable, "-m", "app.workers.analysis_task", job_id], env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix")
    deadline = time.monotonic() + timeout
    reason = None
    try:
        while process.poll() is None:
            if stopping.wait(5):
                # Leave the lease for restart recovery, never call work succeeded.
                reason = "shutdown"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            with SessionLocal() as db:
                if not queue.heartbeat(db, job_id, token):
                    record = db.get(AnalysisJob, job_id)
                    if record and record.status in queue.FINAL:
                        continue  # allow final provenance writes to finish
                    reason = "cancelled" if record and record.cancel_requested else "lost"
                    break
    except Exception:
        reason = "lost"
        log.error("Analysis worker lost database connectivity job_id=%s", job_id)
    finally:
        stop_process(process)
    if reason not in {"lost", "shutdown"}:
        with SessionLocal() as db:
            record = db.get(AnalysisJob, job_id, with_for_update=True)
            if record and record.status == "running" and record.lease_owner == token:
                state = "cancelled" if record.cancel_requested else "failed"
                record.status = state
                record.document = {**record.document, "status": state,
                    "error": "Analisis dibatalkan." if state == "cancelled" else "Analisis melampaui batas sumber daya atau tidak dapat diproses.",
                    "status_code": 408 if reason == "timeout" else 500}
                db.commit()
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            if not run_one(stopping):
                stopping.wait(2)
        except Exception:
            log.error("Analysis queue temporarily unavailable")
            stopping.wait(5)


if __name__ == "__main__":
    main()
