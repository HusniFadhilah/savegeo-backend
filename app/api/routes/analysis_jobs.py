"""Async analysis jobs for long-running GEE workflows.

Public reverse proxies commonly cap a single HTTP request around 60 seconds.
Carbon, multi-index vegetation, and LULC analyses can legitimately take longer,
so the frontend starts a short job request and polls this route for completion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

import ee
import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import get_current_app_viewer
from app.core.temporal import format_rfc3339, utc_now
from app.core.version import get_product_version
from app.core.viewer_features import ensure_job_feature_access
from app.db.models.analysis_provenance import AnalysisProvenance
from app.db.models.carbon_inference_cache import CarbonInferenceCache
from app.db.session import get_db
from app.db.session import SessionLocal
from app.db.models.uploaded_model import UploadedModel
from app.db.models.user import User
from app.services import (
    carbon_service,
    crop_monitoring_service,
    download_service,
    landcover_fusion_service,
    landcover_service,
    provenance_service,
    output_registry_service,
    vegetation_service,
)
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(tags=["analysis-jobs"])
logger = logging.getLogger(__name__)

_worker_count = max(1, int(os.getenv("ANALYSIS_JOB_WORKERS", "2")))
_executor = ThreadPoolExecutor(max_workers=_worker_count)
_job_slots = BoundedSemaphore(max(_worker_count, int(os.getenv("ANALYSIS_JOB_MAX_INFLIGHT", "12"))))
_ALLOWED_JOB_TYPES = {
    "carbon",
    "carbon_local",
    "carbon_delta",
    "geotiff",
    "vegetation",
    "landcover",
    "landcover_change",
    "landcover_transition",
    "landcover_change_map",
    "landcover_hotspots",
    "landcover_fusion",
    "crop_monitoring",
}
_CARBON_CACHEABLE_TYPES = {"carbon", "carbon_local", "carbon_delta"}
_CARBON_CACHE_SCHEMA = 2
_UPSTREAM_CONNECTION_ERROR = (
    "Koneksi ke layanan data eksternal terputus sebelum respons diterima. "
    "Coba jalankan ulang analisis; jika berulang, kecilkan AOI/rentang waktu "
    "atau cek koneksi ke Earth Engine/penyedia raster."
)


def _jobs_dir() -> Path:
    base = Path(get_settings().upload_dir).resolve().parent / "jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _job_path(job_id: str) -> Path:
    if not job_id or any(ch not in "0123456789abcdef-" for ch in job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    return _jobs_dir() / f"{job_id}.json"


def job_owner_key(viewer: object) -> str:
    return f"user:{viewer.id}" if isinstance(viewer, User) else f"admin:{viewer.id}"  # type: ignore[attr-defined]


def viewer_owns_job(job_id: str, viewer: object, created_by_user_id: int | None = None) -> bool:
    """Whether ``viewer`` submitted analysis ``job_id``.

    The job file records the submitting account for both users and admins.
    ``created_by_user_id`` from provenance is a fallback for user jobs whose
    status file is unavailable.
    """
    try:
        path = _job_path(job_id)
    except HTTPException:
        return False
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f).get("owner") == job_owner_key(viewer)
    return isinstance(viewer, User) and created_by_user_id is not None and created_by_user_id == viewer.id


def _now() -> str:
    return format_rfc3339(utc_now())


def _write_job(job_id: str, data: dict[str, Any]) -> None:
    path = _job_path(job_id)
    if path.exists() and "owner" not in data:
        with path.open("r", encoding="utf-8") as previous_file:
            previous_owner = json.load(previous_file).get("owner")
        if previous_owner:
            data = {**data, "owner": previous_owner}
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


def _carbon_cache_ttl(job_type: str) -> int:
    settings = get_settings()
    configured = (
        settings.carbon_local_cache_ttl_seconds
        if job_type == "carbon_local"
        else settings.carbon_gee_cache_ttl_seconds
    )
    ttl = max(60, int(configured))
    # GEE-backed carbon results can contain temporary Map IDs. Keep the TTL
    # below the documented expiry window even if an env value is misconfigured.
    return ttl if job_type == "carbon_local" else min(ttl, 3600)


def _carbon_cache_key(db: Session, job_type: str, payload: dict[str, Any], owner: str) -> str:
    """Hash exact inputs and model identity so AOIs/years never share results."""
    model_name = str(payload.get("model_name") or "")
    model_identity: dict[str, Any] = {"name": model_name}
    if model_name:
        uploaded = db.query(UploadedModel).filter_by(name=model_name, model_type="carbon").first()
        if uploaded is not None:
            model_identity.update({
                "id": uploaded.id,
                "version": uploaded.version,
                "updated_at": uploaded.updated_at.isoformat() if uploaded.updated_at else None,
                "file_size_kb": uploaded.file_size_kb,
                "metadata": uploaded.metadata_json or {},
            })
            try:
                path = Path(uploaded.filepath)
                if path.is_file():
                    stat = path.stat()
                    model_identity["file_signature"] = [stat.st_size, stat.st_mtime_ns]
            except OSError:
                pass
        else:
            from app.registries.carbon_pool_models import get_builtin_carbon_model

            builtin = get_builtin_carbon_model(model_name, db=db)
            if builtin:
                model_identity.update({"version": builtin.get("version"), "metadata": builtin.get("metadata_json")})

    encoded = json.dumps(
        {
            "type": job_type,
            "owner": owner,
            "cache_schema": _CARBON_CACHE_SCHEMA,
            "application_version": get_product_version(),
            "model": model_identity,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _upsert_carbon_cache(
    db: Session,
    *,
    cache_key: str,
    owner: str,
    job_id: str,
    job_type: str,
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Keep cache writes best-effort so cache storage cannot fail an analysis."""
    try:
        now = datetime.now(UTC)
        entry = db.get(CarbonInferenceCache, cache_key)
        if entry is None:
            entry = CarbonInferenceCache(
                cache_key=cache_key,
                owner_key=owner,
                job_id=job_id,
                job_type=job_type,
                status=status,
                result=result,
                expires_at=now + timedelta(seconds=_carbon_cache_ttl(job_type)),
                created_at=now,
                updated_at=now,
            )
            db.add(entry)
        else:
            entry.owner_key = owner
            entry.job_id = job_id
            entry.job_type = job_type
            entry.status = status
            entry.result = result
            entry.expires_at = now + timedelta(seconds=_carbon_cache_ttl(job_type))
            entry.updated_at = now
        db.commit()
    except Exception:  # noqa: BLE001 - analysis must succeed if optional caching is unavailable
        db.rollback()
        logger.warning("Carbon inference cache write failed", exc_info=True)


def _cache_entry_is_fresh(entry: CarbonInferenceCache) -> bool:
    expires_at = entry.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)


def _get_carbon_cache(db: Session, cache_key: str) -> CarbonInferenceCache | None:
    """Missing cache schema should fall back to the normal inference path."""
    try:
        return db.get(CarbonInferenceCache, cache_key)
    except Exception:  # noqa: BLE001 - caching is an optimization, not a prerequisite
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            rollback()
        logger.warning("Carbon inference cache read failed", exc_info=True)
        return None


def _complete_cached_job(
    db: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    owner: str,
    result: dict[str, Any],
    viewer: object,
) -> dict[str, Any]:
    """Materialize a normal, pollable job from a cached result."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    provenance_service.create_analysis_provenance(
        db,
        analysis_id=job_id,
        analysis_type=job_type,
        payload=payload,
        user_id=viewer.id if isinstance(viewer, User) else None,
    )
    _write_job(job_id, {
        "job_id": job_id,
        "type": job_type,
        "status": "succeeded",
        "created_at": _now(),
        "started_at": format_rfc3339(now),
        "finished_at": format_rfc3339(now),
        "cache_hit": True,
        "owner": owner,
        "result": result,
    })
    output_registry_service.register_action_outputs(db, job_id, result)
    provenance_service.update_analysis_provenance(
        db, job_id, status="succeeded", started_at=now, completed_at=now
    )
    return {"job_id": job_id, "status": "succeeded", "status_url": f"/analysis-jobs/{job_id}", "cache_hit": True}


def _run_job(
    job_id: str,
    job_type: str,
    payload: dict[str, Any],
    cache_key: str | None = None,
    owner: str | None = None,
) -> None:
    _write_job(job_id, {"job_id": job_id, "type": job_type, "status": "running", "started_at": _now()})
    db = SessionLocal()
    if cache_key and owner:
        _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="running")
    provenance_service.update_analysis_provenance(
        db,
        job_id,
        status="running",
        started_at=datetime.now(UTC),
    )
    try:
        if job_type == "carbon":
            result = carbon_service.analyze_carbon(db, payload)
        elif job_type == "carbon_local":
            result = carbon_service.analyze_carbon_local(db, payload)
        elif job_type == "carbon_delta":
            result = carbon_service.analyze_carbon_delta(db, payload)
        elif job_type == "geotiff":
            result = download_service.download_geotiff(db, payload)
        elif job_type == "vegetation":
            result = vegetation_service.analyze_vegetation(db, payload)
        elif job_type in {"landcover", "landcover_change"}:
            result = landcover_service.analyze_landcover(payload)
        elif job_type == "landcover_transition":
            result = landcover_service.analyze_landcover_transition(payload)
        elif job_type == "landcover_change_map":
            result = landcover_service.analyze_landcover_change_map(payload)
        elif job_type == "landcover_hotspots":
            result = landcover_service.analyze_landcover_hotspots(payload)
        elif job_type == "landcover_fusion":
            result = landcover_fusion_service.analyze_landcover_fusion(payload)
        elif job_type == "crop_monitoring":
            result = crop_monitoring_service.run_crop_monitoring(db, payload)
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
        if cache_key and owner:
            # Result payloads have already been JSON-serialized by the job store.
            cached_result = json.loads(json.dumps(result, ensure_ascii=False, default=str))
            _upsert_carbon_cache(
                db, cache_key=cache_key, owner=owner, job_id=job_id,
                job_type=job_type, status="completed", result=cached_result,
            )
        output_registry_service.register_action_outputs(db, job_id, result)
        provenance_service.update_analysis_provenance(
            db,
            job_id,
            status="succeeded",
            completed_at=datetime.now(UTC),
        )
    except AnalysisError as exc:
        if cache_key and owner:
            _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="failed")
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "failed",
                "finished_at": _now(),
                "error": public_analysis_error(exc),
                "status_code": exc.status_code,
            },
        )
        provenance_service.update_analysis_provenance(
            db,
            job_id,
            status="failed",
            error_code=f"analysis_{exc.status_code}",
            completed_at=datetime.now(UTC),
        )
    except ee.EEException:
        if cache_key and owner:
            _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="failed")
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "failed",
                "finished_at": _now(),
                "error": "Earth Engine tidak dapat memproses analisis. Periksa AOI dan parameter lalu coba lagi.",
                "status_code": 400,
            },
        )
        provenance_service.update_analysis_provenance(
            db, job_id, status="failed", error_code="earth_engine_error", completed_at=datetime.now(UTC)
        )
    except requests.RequestException as exc:
        if cache_key and owner:
            _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="failed")
        logger.warning("Analysis job %s upstream connection failed: %s", job_id, type(exc).__name__)
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "failed",
                "finished_at": _now(),
                "error": _UPSTREAM_CONNECTION_ERROR,
                "status_code": 503,
            },
        )
        provenance_service.update_analysis_provenance(
            db, job_id, status="failed", error_code="upstream_unavailable", completed_at=datetime.now(UTC)
        )
    except Exception:
        if cache_key and owner:
            _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="failed")
        logger.error("Analysis job %s failed", job_id)
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "failed",
                "finished_at": _now(),
                "error": "Analisis gagal. Coba lagi atau hubungi administrator.",
                "status_code": 500,
            },
        )
        provenance_service.update_analysis_provenance(
            db, job_id, status="failed", error_code="internal_error", completed_at=datetime.now(UTC)
        )
    finally:
        db.close()


@router.post("/analysis-jobs", dependencies=[Depends(get_current_app_viewer)])
def create_analysis_job(
    request: Request,
    data: dict[str, Any] = Body(...),
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    job_type = str(data.get("type") or "")
    payload = data.get("payload")
    if job_type not in _ALLOWED_JOB_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported analysis job type: {job_type}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Missing required field: payload")
    ensure_job_feature_access(viewer, job_type, db, payload)

    # Pure local/reference jobs do not need an Earth Engine session. Keep the
    # gate for all GEE workflows, but allow COG/SoilGrids references to work
    # during credential outages and in offline deployments.
    needs_ee = job_type not in {"carbon_local"}
    if job_type == "carbon_local" and payload.get("model_name") == "ibm_granite_geospatial_biomass":
        # Granite's local inference stage consumes an HLS composite downloaded
        # from Earth Engine, so it still needs the EE session initialized.
        needs_ee = True
    if job_type == "carbon":
        from app.registries.carbon_dataset_registry import get_external_carbon_meta
        dataset = str(payload.get("reference_dataset") or "")
        meta = get_external_carbon_meta(dataset)
        direct = bool(payload.get("reference_only")) or not payload.get("model_name")
        needs_ee = not (direct and meta and meta.get("ingestion_method") in {"cog_rasterio", "soilgrids_rest"})
    owner = job_owner_key(viewer)
    cache_key = _carbon_cache_key(db, job_type, payload, owner) if job_type in _CARBON_CACHEABLE_TYPES else None
    if cache_key:
        cached = _get_carbon_cache(db, cache_key)
        if cached is not None and _cache_entry_is_fresh(cached):
            if cached.status == "completed" and isinstance(cached.result, dict):
                return _complete_cached_job(
                    db, job_type=job_type, payload=payload, owner=owner,
                    result=cached.result, viewer=viewer,
                )
            if cached.status in {"queued", "running"}:
                try:
                    pending = _read_job(cached.job_id)
                    if pending.get("owner") == owner and pending.get("status") in {"queued", "running"}:
                        return {
                            "job_id": cached.job_id,
                            "status": pending["status"],
                            "status_url": f"/analysis-jobs/{cached.job_id}",
                            "cache_hit": True,
                        }
                except HTTPException:
                    pass

    if needs_ee and not getattr(request.app.state, "ee_initialized", False):
        raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi. Cek kredensial di Admin Panel.")

    if not _job_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Terlalu banyak analisis sedang diproses. Coba lagi sebentar.",
            headers={"Retry-After": "30"},
        )

    job_id = str(uuid.uuid4())
    try:
        provenance_service.create_analysis_provenance(
            db,
            analysis_id=job_id,
            analysis_type=job_type,
            payload=payload,
            user_id=viewer.id if isinstance(viewer, User) else None,
        )
    except ValueError as exc:
        db.rollback()
        _job_slots.release()
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATETIME", "message": str(exc)}) from exc
    except Exception:
        _job_slots.release()
        raise
    try:
        output_registry_service.register_action_outputs(db, job_id)
        _write_job(job_id, {"job_id": job_id, "type": job_type, "status": "queued", "created_at": _now(), "owner": owner})
        if cache_key:
            _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="queued")
        future = _executor.submit(_run_job, job_id, job_type, payload, cache_key, owner)
        future.add_done_callback(lambda _future: _job_slots.release())
    except Exception:
        if cache_key:
            _upsert_carbon_cache(db, cache_key=cache_key, owner=owner, job_id=job_id, job_type=job_type, status="failed")
        _job_slots.release()
        raise
    return {"job_id": job_id, "status": "queued", "status_url": f"/analysis-jobs/{job_id}"}


@router.get("/analysis-jobs/{job_id}")
def get_analysis_job(
    job_id: str,
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    job = _read_job(job_id)
    owner = f"user:{viewer.id}" if isinstance(viewer, User) else f"admin:{viewer.id}"
    if job.get("owner") != owner:
        raise HTTPException(status_code=404, detail="Job not found")
    provenance = db.get(AnalysisProvenance, job_id)
    ensure_job_feature_access(
        viewer,
        str(job.get("type") or ""),
        db,
        provenance.parameters if provenance is not None else None,
    )
    job = {key: value for key, value in job.items() if key != "owner"}
    if job.get("status") == "failed":
        status_code = int(job.get("status_code") or 500)
        return {k: v for k, v in job.items() if k != "status_code"} | {"http_status": status_code}
    return job
