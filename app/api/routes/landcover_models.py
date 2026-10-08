"""Admin configuration and asynchronous EE classifier training."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

import ee
from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_ee
from app.core.security import require_permission
from app.services import landcover_model_service as service
from app.services.gee_common import create_geometry_from_payload

router = APIRouter(prefix="/admin/landcover-models", tags=["landcover-models"])
_executor = ThreadPoolExecutor(max_workers=1)
_slot = BoundedSemaphore(1)


@router.get("", dependencies=[Depends(require_permission("model.read"))])
def get_config():
    config = service.refresh_model_catalog()
    return {"config": config, "models": [{"key": key, **service.LAND_COVER_DATASET_OPTIONS[key]} for key in sorted(service.MODEL_KEYS)]}


@router.put("", dependencies=[Depends(require_permission("model.write"))])
def update_config(config: dict = Body(...)):
    try:
        return {"config": service.write_model_config(config), "message": "Konfigurasi model land cover disimpan."}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _job_path(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=400, detail="ID training tidak valid.")
    directory = service.model_config_path().parent / "landcover-training"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{job_id}.json"


def _save_job(job_id: str, value: dict):
    path = _job_path(job_id)
    fd, temporary = tempfile.mkstemp(prefix=f"{job_id}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _train(job_id: str, body: dict):
    try:
        _save_job(job_id, {"status": "sampling", "model": body["model"]})
        config = service.read_model_config().get(body["model"], {})
        region = create_geometry_from_payload(body["aoi"]) if body.get("aoi") else None
        classifier, metadata = service.build_reference_classifier(body["model"], config, region)
        task = ee.batch.Export.classifier.toAsset(classifier, f"savegeo_lulc_{job_id[:8]}", body["asset_id"])
        task.start()
        _save_job(job_id, {"status": "exporting", "model": body["model"], "task_id": task.id, "asset_id": body["asset_id"], "model_version": body["model_version"], "training_metadata": metadata})
    except Exception as exc:
        _save_job(job_id, {"status": "failed", "error": str(exc), "model": body["model"]})
    finally:
        _slot.release()


@router.post("/train", dependencies=[Depends(require_permission("model.write")), Depends(require_ee)])
def train(body: dict = Body(...)):
    if body.get("model") not in service.RF_KEYS:
        raise HTTPException(status_code=400, detail="Training EE tersedia untuk AlphaEarth_RF dan Sentinel1_2_RF.")
    asset = body.get("asset_id", "")
    if not isinstance(asset, str) or not service._ASSET_PATTERN.fullmatch(asset) or ".." in asset or "{" in asset:
        raise HTTPException(status_code=400, detail="Asset tujuan harus projects/.../assets/... tanpa placeholder.")
    version = body.get("model_version")
    if not isinstance(version, str) or not 1 <= len(version) <= 100:
        raise HTTPException(status_code=400, detail="Isi versi model (maksimal 100 karakter).")
    if not _slot.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Satu training sedang berjalan; tunggu hingga selesai.")
    job_id = uuid.uuid4().hex
    try:
        _save_job(job_id, {"status": "queued", "model": body["model"]})
        _executor.submit(_train, job_id, body)
    except Exception:
        _slot.release()
        raise
    return {"job_id": job_id, "status": "queued"}


@router.get("/training/{job_id}", dependencies=[Depends(require_permission("model.read"))])
def training_status(job_id: str):
    path = _job_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Training tidak ditemukan.")
    result = json.loads(path.read_text(encoding="utf-8"))
    if result["status"] == "exporting":
        status = ee.data.getTaskStatus(result["task_id"])[0]
        if status["state"] == "COMPLETED":
            result.update({"status": "completed", "config_entry": {
                "classifier_asset": result["asset_id"], "model_version": result["model_version"],
                "class_encoding": "indices", "validation_kind": "reference_labels",
                "accuracy_note": result["training_metadata"]["quality_note"],
                "training_metadata": result["training_metadata"],
                "annual_asset_template": None, "classes": service.MAPBIOMAS_DETAIL_LEGEND,
                "class_schema": "MAPBIOMAS_INDONESIA_41_13",
            }})
            _save_job(job_id, result)
        elif status["state"] in {"FAILED", "CANCELLED"}:
            result.update({"status": "failed", "error": status.get("error_message", status["state"])})
            _save_job(job_id, result)
    return result
