"""Keep checkpointed green-ranking jobs running outside the API container."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("UPLOAD_DIR", "/app/var/uploads")) / "green-rankings"
ENABLED = ROOT / "worker.enable"
PAUSED = ROOT / "worker.pause"
SUPERVISOR = ROOT / "green-ranking-job-supervisor.py"
API_READY_URL = os.environ.get("GREEN_RANKING_API_READY_URL", "http://api:8086/api/ready")
POLL_SECONDS = max(10, int(os.environ.get("GREEN_RANKING_SUPERVISOR_POLL_SECONDS", "30")))
LOGGER = logging.getLogger("green_rankings.supervisor")
STOP = False


def _handle_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def _api_is_ready() -> bool:
    try:
        with urllib.request.urlopen(API_READY_URL, timeout=5) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _reset_checkpoint_leases() -> int:
    """Clear stale leases only for worker runs saved in their durable checkpoints."""
    database = ROOT / "rankings.sqlite3"
    if not database.is_file():
        LOGGER.info("no ranking database exists yet; lease recovery skipped")
        return 0

    reset = 0
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        for checkpoint in ROOT.glob("green-ranking-*.json"):
            try:
                state = json.loads(checkpoint.read_text(encoding="utf-8"))
                if state.get("status") != "running":
                    continue
                level = str(state.get("level", ""))
                year = int(state.get("year", 0))
                run_id = str(state.get("run_id", ""))
                if level not in {"city", "district", "village"} or not run_id:
                    continue
                row = connection.execute(
                    "SELECT owner, done FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None or row[1]:
                    continue
                owner_pattern = rf"admin:green-ranking-refresh-\d{{8}}-{re.escape(level)}-{year}"
                if not re.fullmatch(owner_pattern, str(row[0] or "")):
                    LOGGER.warning("checkpoint owner did not match; lease left unchanged (%s)", checkpoint.name)
                    continue
                changed = connection.execute(
                    "UPDATE runs SET lease=0 WHERE id=? AND done=0 AND lease>0", (run_id,)
                ).rowcount
                reset += int(changed or 0)
            except (OSError, ValueError, TypeError, sqlite3.Error):
                LOGGER.exception("could not recover checkpoint lease (%s)", checkpoint.name)
        connection.commit()
    LOGGER.info("recovered %s abandoned ranking lease(s) from checkpoints", reset)
    return reset


def _ensure_jobs() -> None:
    if not SUPERVISOR.is_file():
        LOGGER.error("ranking job supervisor is missing at %s", SUPERVISOR)
        return
    try:
        result = subprocess.run(
            [sys.executable, "-u", str(SUPERVISOR)],
            cwd="/app",
            check=False,
            timeout=90,
        )
        if result.returncode:
            LOGGER.warning("ranking job supervisor exited with status %s", result.returncode)
    except subprocess.TimeoutExpired:
        LOGGER.warning("ranking job supervisor exceeded its 90-second limit")
    except OSError:
        LOGGER.exception("could not run ranking job supervisor")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    ROOT.mkdir(parents=True, exist_ok=True)
    LOGGER.info("ranking supervisor started in standby")
    prepared = False

    while not STOP:
        if not ENABLED.exists() or PAUSED.exists():
            time.sleep(5)
            continue
        if not _api_is_ready():
            LOGGER.info("waiting for API readiness at %s", API_READY_URL)
            time.sleep(10)
            continue
        if not prepared:
            _reset_checkpoint_leases()
            prepared = True
        _ensure_jobs()
        time.sleep(POLL_SECONDS)

    LOGGER.info("ranking supervisor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
