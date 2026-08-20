"""Async analysis jobs for long-running GEE workflows.

Public reverse proxies commonly cap a single HTTP request around 60 seconds.
Carbon, multi-index vegetation, and LULC analyses can legitimately take longer,
so the frontend starts a short job request and polls this route for completion.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ee
from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_ee
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services import carbon_service, landcover_service, vegetation_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["analysis-jobs"])
logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=int(os.getenv("ANALYSIS_JOB_WORKERS", "2")))
_ALLOWED_JOB_TYPES = {"carbon", "carbon_local", "vegetation", "landcover"}


def _jobs_dir() -> Path:
    base = Path(get_settings().upload_dir).resolve().parent / "jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _job_path(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef-" for ch in job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    return _jobs_dir() / f"{job_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_job(job_id: str, data: dict[str, Any]) -> None:
    path = _job_path(job_id)
    data = {**data, "updated_at": _now()}
    fd, tmp_name = tempfile.mkstemp(prefix=f"{job_id}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _read_job(job_id: str) -> dict[str, Any]:
    path = _job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _run_job(job_id: str, job_type: str, payload: dict[str, Any]) -> None:
    _write_job(job_id, {"job_id": job_id, "type": job_type, "status": "running", "started_at": _now()})
    db = SessionLocal()
    try:
        if job_type == "carbon":
            result = carbon_service.analyze_carbon(db, payload)
        elif job_type == "carbon_local":
            result = carbon_service.analyze_carbon_local(db, payload)
        elif job_type == "vegetation":
            result = vegetation_service.analyze_vegetation(db, payload)
        elif job_type == "landcover":
            result = landcover_service.analyze_landcover(payload)
        else:  # defensive; validated before submit
            raise ValueError(f"Unsupported job type: {job_type}")
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "succeeded",
                "started_at": _read_job(job_id).get("started_at"),
                "finished_at": _now(),
                "result": result,
            },
        )
    except AnalysisError as exc:
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "failed",
                "finished_at": _now(),
                "error": str(exc),
                "status_code": exc.status_code,
            },
        )
    except ee.EEException as exc:
        _write_job(
            job_id,
            {"job_id": job_id, "type": job_type, "status": "failed", "finished_at": _now(), "error": str(exc), "status_code": 400},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Analysis job %s failed", job_id)
        _write_job(
            job_id,
            {"job_id": job_id, "type": job_type, "status": "failed", "finished_at": _now(), "error": str(exc), "status_code": 500},
        )
    finally:
        db.close()


@router.post("/analysis-jobs", dependencies=[Depends(require_ee)])
def create_analysis_job(data: dict[str, Any] = Body(...)):
    job_type = str(data.get("type") or "")
    payload = data.get("payload")
    if job_type not in _ALLOWED_JOB_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported analysis job type: {job_type}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Missing required field: payload")

    job_id = str(uuid.uuid4())
    _write_job(job_id, {"job_id": job_id, "type": job_type, "status": "queued", "created_at": _now()})
    _executor.submit(_run_job, job_id, job_type, payload)
    return {"job_id": job_id, "status": "queued", "status_url": f"/analysis-jobs/{job_id}"}


@router.get("/analysis-jobs/{job_id}")
def get_analysis_job(job_id: str):
    job = _read_job(job_id)
    if job.get("status") == "failed":
        status_code = int(job.get("status_code") or 500)
        return {k: v for k, v in job.items() if k != "status_code"} | {"http_status": status_code}
    return job
