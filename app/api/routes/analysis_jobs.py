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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
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
    config_service,
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
from app.services.carbon_retry import run_carbon_with_retry
from app.services.aoi_reference import resolve_known_aoi
from app.services.analysis_requirements import requires_earth_engine
from app.services.analysis_preflight import assess_result_quality, preflight as run_analysis_preflight
from app.services import analysis_queue
from app.db.models.analysis_job import AnalysisJob
from app.core.bounded_json import read_typed_json_object
from app.schemas.analysis_jobs import AnalysisJobRequest

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
_GEE_SERVING_JOB_TYPES = {
    "carbon", "carbon_local", "carbon_delta", "vegetation", "landcover",
    "landcover_change", "landcover_transition", "landcover_change_map",
    "landcover_hotspots", "landcover_fusion",
}
_CARBON_CACHE_SCHEMA = 5
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
    if analysis_queue.enabled():
        with SessionLocal() as db:
            record = db.get(AnalysisJob, job_id)
            if record is not None:
                return record.owner == job_owner_key(viewer)
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


def _serving_code_identity() -> dict[str, Any]:
    """Content-address the server code that defines Earth Engine analysis serving."""
    from app.services.model_serving_identity import current_identity

    return current_identity()


def _verify_serving_code_identity(payload: dict[str, Any]) -> dict[str, Any] | None:
    expected = payload.get("_server_serving_revision")
    if not isinstance(expected, dict):
        return None
    from app.services.model_serving_identity import matches

    if not matches(expected):
        raise AnalysisError(
            "Versi serving GEE berubah setelah job dibuat. Job dihentikan agar model tidak berjalan dengan kode yang berbeda; kirim ulang analisis.",
            409,
        )
    return expected


def _write_job(job_id: str, data: dict[str, Any]) -> None:
    if analysis_queue.enabled():
        with SessionLocal() as db:
            if analysis_queue.write_document(db, job_id, {**data, "updated_at": _now()},
                    os.getenv("SAVEGEO_ANALYSIS_LEASE")):
                return
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
    if analysis_queue.enabled():
        with SessionLocal() as db:
            record = db.get(AnalysisJob, job_id)
            if record is not None:
                return record.document
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


def _model_pin_for_job(db: Session, job_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve selected DB-backed model to an immutable identity at submission."""
    if job_type in {"landcover", "landcover_change", "landcover_transition", "landcover_change_map", "landcover_hotspots", "landcover_fusion"}:
        model_key = str(payload.get("dataset") or payload.get("model_key") or "")
        from app.services.landcover_model_service import MODEL_KEYS
        if model_key not in MODEL_KEYS:
            return None
        candidates = db.query(UploadedModel).filter_by(model_type="landcover", lifecycle_state="active", is_active=True).all()
        row = next((candidate for candidate in candidates
                    if (candidate.metadata_json or {}).get("format") == "gee_config_release"
                    and (candidate.metadata_json or {}).get("model_key") == model_key), None)
        if row is None:
            from app.services.landcover_model_service import read_model_config
            pending = read_model_config().get(model_key, {})
            if pending.get("classifier_asset") or pending.get("annual_asset_template"):
                raise HTTPException(409, detail={"code": "model_not_approved", "message": "Model GEE tutupan lahan belum dievaluasi, direview independen, dan diaktifkan."})
            return None
        try:
            from app.services import model_lifecycle
            evaluation = model_lifecycle.latest_evaluation(db, row)
            if not model_lifecycle.has_current_approval(db, row, evaluation):
                raise ValueError("release approval is not current")
            model_lifecycle.artifact_path(row)
        except Exception as exc:  # noqa: BLE001 - no unverified GEE artifact can be pinned
            raise HTTPException(409, detail={"code": "model_not_approved", "message": "Model GEE belum memiliki approval aktif yang valid."}) from exc
        return {"id": row.id, "name": row.name, "version": row.version,
                "artifact_sha256": row.artifact_sha256, "approval_digest": row.approval_digest,
                "format": "gee_config_release", "model_key": model_key}
    model_name = payload.get("model_name")
    if job_type not in {"carbon", "carbon_local", "carbon_delta"}:
        return None
    row = None
    if isinstance(model_name, str) and model_name:
        row = db.query(UploadedModel).filter_by(name=model_name, model_type="carbon").first()
    else:
        # The automatic litter/deadwood adapter selects the active saved model
        # with the strongest recorded spatial holdout score. Resolve that same
        # choice now so a queued job cannot silently switch to a later model.
        dataset_key = payload.get("reference_dataset")
        if job_type == "carbon" and dataset_key in {"TROPICAL_LITTER_REFERENCE", "TROPICAL_DEADWOOD_REFERENCE"} and not payload.get("reference_only") and not payload.get("load_only"):
            candidates = [
                candidate for candidate in db.query(UploadedModel).filter_by(
                    model_type="carbon", lifecycle_state="active", is_active=True
                ).all()
                if (candidate.metadata_json or {}).get("method") == "saved_reference_ensemble"
                and dataset_key in (candidate.metadata_json or {}).get("compatible_reference_datasets", [])
            ]
            model_row = max(candidates, key=lambda candidate: float(
                ((candidate.metadata_json or {}).get("validation_metrics") or {}).get("r2", -1e9)
            )) if candidates else None
            row = model_row
        else:
            return None
    if row is None:
        return None  # code-defined built-ins are pinned by the application revision
    if row.lifecycle_state != "active" or not row.is_active:
        raise HTTPException(409, detail={"code": "model_not_active", "message": "Versi model yang dipilih tidak aktif."})
    metadata = row.metadata_json or {}
    artifact_format = metadata.get("format")
    try:
        if artifact_format == "onnx":
            from app.repositories.uploaded_model_repo import get_active_model_path

            if get_active_model_path(db, "carbon", row.name) is None:
                raise ValueError("active ONNX artifact could not be verified")
        elif artifact_format == "gee_saved_ensemble":
            from app.services import model_lifecycle

            evaluation = model_lifecycle.latest_evaluation(db, row)
            if not model_lifecycle.has_current_approval(db, row, evaluation):
                raise ValueError("GEE model review is not current")
            model_lifecycle.artifact_path(row)
        else:
            return None
    except Exception as exc:  # noqa: BLE001 - reject unverifiable serving identities
        raise HTTPException(409, detail={"code": "model_not_approved", "message": "Artefak/approval model tidak dapat diverifikasi."}) from exc
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "artifact_sha256": row.artifact_sha256,
        "approval_digest": row.approval_digest,
        "format": artifact_format,
    }


def _verify_job_model_pin(db: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    pin = payload.get("_server_model_pin")
    if not isinstance(pin, dict):
        return None
    row = db.get(UploadedModel, pin.get("id"))
    if (
        row is None
        or row.name != pin.get("name")
        or row.version != pin.get("version")
        or row.artifact_sha256 != pin.get("artifact_sha256")
        or row.approval_digest != pin.get("approval_digest")
        or row.lifecycle_state != "active"
        or not row.is_active
    ):
        raise AnalysisError("Versi model berubah setelah job dibuat. Jalankan ulang analisis agar memakai versi yang disetujui.", 409)
    try:
        if pin.get("format") == "onnx":
            from app.repositories.uploaded_model_repo import get_active_model_path

            valid = get_active_model_path(db, "carbon", row.name) is not None
        elif pin.get("format") == "gee_saved_ensemble":
            from app.services import model_lifecycle

            evaluation = model_lifecycle.latest_evaluation(db, row)
            valid = model_lifecycle.has_current_approval(db, row, evaluation)
            model_lifecycle.artifact_path(row)
        elif pin.get("format") == "gee_config_release":
            from app.services import model_lifecycle

            evaluation = model_lifecycle.latest_evaluation(db, row)
            valid = model_lifecycle.has_current_approval(db, row, evaluation)
            model_lifecycle.artifact_path(row)
            valid = valid and (row.metadata_json or {}).get("model_key") == pin.get("model_key")
        else:
            valid = False
    except Exception as exc:  # noqa: BLE001 - checksum/review drift blocks serving
        raise AnalysisError("Artefak model tidak lolos pemeriksaan integritas saat job dijalankan.", 409) from exc
    if not valid:
        raise AnalysisError("Approval versi model tidak lagi berlaku saat job dijalankan.", 409)
    return pin


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
                "artifact_sha256": uploaded.artifact_sha256,
                "lifecycle_state": uploaded.lifecycle_state,
                "approval_digest": uploaded.approval_digest,
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

    analysis_defaults = config_service.get_analysis_defaults(db)
    visualization_identity = {
        "carbon_vis_min": analysis_defaults["carbon_vis_min"],
        "carbon_vis_max": analysis_defaults["carbon_vis_max"],
        "carbon_vis_palette": analysis_defaults["carbon_vis_palette"],
        "carbon_legend_bins": analysis_defaults["carbon_legend_bins"],
        "carbon_pool_visualization": analysis_defaults["carbon_pool_visualization"],
    }
    encoded = json.dumps(
        {
            "type": job_type,
            "owner": owner,
            "cache_schema": _CARBON_CACHE_SCHEMA,
            "application_version": get_product_version(),
            "model": model_identity,
            "visualization": visualization_identity,
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
    result = json.loads(json.dumps(result))
    info = result.get("model_info", {})
    if job_type == "carbon" and info.get("annual_summary_persisted"):
        info["annual_summary_source"] = "database"
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    provenance_service.create_analysis_provenance(
        db,
        analysis_id=job_id,
        analysis_type=job_type,
        payload=payload,
        user_id=viewer.id if isinstance(viewer, User) else None,
    )
    result = {**result, "quality_assessment": assess_result_quality(job_type, result)}
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
    provenance_service.record_analysis_execution(
        db, job_id, result,
        payload.get("_server_model_pin") if isinstance(payload.get("_server_model_pin"), dict) else None,
        payload.get("_server_serving_revision") if isinstance(payload.get("_server_serving_revision"), dict) else None,
    )
    provenance_service.update_analysis_provenance(
        db, job_id, status="succeeded", started_at=now, completed_at=now
    )
    return {"job_id": job_id, "status": "succeeded", "status_url": f"/analysis-jobs/{job_id}", "cache_hit": True}


def _analyze_carbon_job(db: Session, payload: dict) -> dict:
    return carbon_service.analyze_carbon(db, resolve_known_aoi(payload))


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
        pinned_model = _verify_job_model_pin(db, payload)
        serving_revision = _verify_serving_code_identity(payload) if job_type in _GEE_SERVING_JOB_TYPES else None
        if job_type in {"vegetation", "landcover", "landcover_change", "landcover_transition", "landcover_change_map", "landcover_hotspots", "landcover_fusion"}:
            payload = resolve_known_aoi(payload)
        if job_type == "carbon":
            payload = {**payload, "_request_owner": owner}
            result = run_carbon_with_retry(
                lambda: _analyze_carbon_job(db, payload), job_id=job_id,
            )
        elif job_type == "carbon_local":
            result = carbon_service.analyze_carbon_local(db, payload)
        elif job_type == "carbon_delta":
            result = carbon_service.analyze_carbon_delta(db, payload)
        elif job_type == "geotiff":
            result = download_service.download_geotiff(db, {**payload, "_request_owner": owner})
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
            result = crop_monitoring_service.run_crop_monitoring(db, payload, owner)
        else:  # defensive; validated before submit
            raise ValueError(f"Unsupported job type: {job_type}")
        if isinstance(result, dict):
            result = {**result, "quality_assessment": assess_result_quality(job_type, result)}
        if cache_key and owner:
            # Result payloads have already been JSON-serialized by the job store.
            cached_result = json.loads(json.dumps(result, ensure_ascii=False, default=str))
            _upsert_carbon_cache(
                db, cache_key=cache_key, owner=owner, job_id=job_id,
                job_type=job_type, status="completed", result=cached_result,
            )
        output_registry_service.register_action_outputs(db, job_id, result)
        provenance_service.record_analysis_execution(db, job_id, result, pinned_model, serving_revision)
        provenance_service.update_analysis_provenance(
            db,
            job_id,
            status="succeeded",
            completed_at=datetime.now(UTC),
        )
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
async def create_analysis_job(
    request: Request,
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    data = await read_typed_json_object(request, AnalysisJobRequest)
    return await run_in_threadpool(_create_analysis_job_sync, request, data, viewer, db)


@router.post("/analysis-jobs/preflight", dependencies=[Depends(get_current_app_viewer)])
async def analysis_preflight(request: Request, viewer: object = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    data = await read_typed_json_object(request, AnalysisJobRequest)
    job_type = data["type"]
    payload = data.get("payload")
    if job_type not in _ALLOWED_JOB_TYPES or not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Jenis analisis atau payload tidak valid untuk preflight.")
    ensure_job_feature_access(viewer, job_type, db, payload)
    result = run_analysis_preflight(job_type, payload)
    if result["status"] == "blocked":
        return {**result, "http_status": 422}
    return result


def _create_analysis_job_sync(request: Request, data: dict[str, Any], viewer: object, db: Session):
    job_type = data["type"]
    payload = data.get("payload")
    if job_type not in _ALLOWED_JOB_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported analysis job type: {job_type}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Missing required field: payload")
    payload = dict(payload)
    # Never trust model identity pins supplied by a browser. The API resolves a
    # current, reviewed identity and stores that server-side on the queued job.
    payload.pop("_server_model_pin", None)
    payload.pop("_server_serving_revision", None)
    ensure_job_feature_access(viewer, job_type, db, payload)
    preflight_report = run_analysis_preflight(job_type, payload)
    if preflight_report["status"] == "blocked":
        first = preflight_report["blocking"][0]
        raise HTTPException(status_code=422, detail={"code": first["code"], "message": first["message"], "preflight": preflight_report})

    model_pin = _model_pin_for_job(db, job_type, payload)
    if model_pin is not None:
        payload["_server_model_pin"] = model_pin
    if job_type in _GEE_SERVING_JOB_TYPES:
        payload["_server_serving_revision"] = _serving_code_identity()

    if job_type in _CARBON_CACHEABLE_TYPES and payload.get("model_name"):
        uploaded = db.query(UploadedModel).filter_by(name=payload["model_name"], model_type="carbon").first()
        if uploaded is not None:
            from app.repositories.uploaded_model_repo import get_active_model_path
            if get_active_model_path(db, "carbon", uploaded.name) is None:
                raise HTTPException(409, detail={"code": "model_not_approved", "message": "Model belum memiliki evaluasi, approval, dan artefak aktif yang valid."})

    # Pure local/reference jobs do not need an Earth Engine session. Keep the
    # gate for all GEE workflows, but allow COG/SoilGrids references to work
    # during credential outages and in offline deployments.
    needs_ee = requires_earth_engine(job_type, payload)
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

    if analysis_queue.enabled():
        record, created = analysis_queue.prepare(db, job_type, payload, owner, cache_key,
            request.headers.get("Idempotency-Key"))
        if created:
            try:
                provenance_service.create_analysis_provenance(db, analysis_id=record.id,
                    analysis_type=job_type, payload=payload,
                    user_id=viewer.id if isinstance(viewer, User) else None)
                output_registry_service.register_action_outputs(db, record.id)
                analysis_queue.activate(db, record.id)
            except Exception:
                db.rollback()
                analysis_queue.write_document(db, record.id, {"job_id": record.id, "type": job_type,
                    "status": "failed", "error": "Pekerjaan tidak dapat disiapkan.", "status_code": 500})
                raise
        return {"job_id": record.id, "status": record.status,
            "status_url": f"/analysis-jobs/{record.id}", "idempotent_replay": not created}

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


@router.post("/analysis-jobs/{job_id}/cancel")
def cancel_analysis_job(job_id: str, viewer: object = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    if not analysis_queue.enabled():
        raise HTTPException(501, "Cancellation requires the durable queue")
    return analysis_queue.cancel(db, job_id, job_owner_key(viewer))
