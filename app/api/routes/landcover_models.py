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
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import require_permission
from app.db.models.admin_user import AdminUser
from app.db.models.uploaded_model import UploadedModel
from app.db.models.audit_log import AuditLog
from app.db.session import get_db
from app.services import landcover_model_service as service
from app.services import gee_model_release_service
from app.services.gee_common import create_geometry_from_payload

router = APIRouter(prefix="/admin/landcover-models", tags=["landcover-models"])
_executor = ThreadPoolExecutor(max_workers=1)
_slot = BoundedSemaphore(1)


@router.get("", dependencies=[Depends(require_permission("model.read"))])
def get_config():
    config = service.refresh_model_catalog()
    return {"config": config, "models": [{"key": key, **service.LAND_COVER_DATASET_OPTIONS[key]} for key in sorted(service.MODEL_KEYS)]}


@router.put("", dependencies=[Depends(require_permission("model.write"))])
def update_config(config: dict = Body(...), admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    try:
        clean = service.validate_model_config(config)
        candidates = []
        for model_key, entry in clean.items():
            if entry.get("classifier_asset") or entry.get("annual_asset_template"):
                candidates.append(gee_model_release_service.register_landcover_config(db, admin.id, model_key, entry))
        saved = service.write_model_config(clean)
        db.commit()
        return {"config": saved, "candidates": [row.to_dict() for row in candidates],
                "message": "Konfigurasi disimpan sebagai kandidat; model GEE baru menunggu evaluasi, review independen, dan aktivasi."}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class LandcoverHoldoutPoint(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    reference_class: int = Field(ge=0, le=999)
    group_id: str = Field(min_length=1, max_length=100)


class LandcoverReleaseEvaluation(BaseModel):
    dataset_name: str = Field(min_length=1, max_length=255)
    dataset_source: str | None = Field(default=None, max_length=1000)
    training_group_ids: list[str] = Field(min_length=1, max_length=10000)
    split_method: str = Field(pattern="^(spatial_holdout|temporal_holdout|independent_dataset)$")
    year: int = Field(ge=1985, le=2100)
    points: list[LandcoverHoldoutPoint] = Field(min_length=10, max_length=5000)


def _landcover_metrics(points: list[dict], observed: dict[str, int], classes: list[int]) -> dict:
    matrix = {actual: {predicted: 0 for predicted in classes} for actual in classes}
    retained = []
    for point in points:
        prediction = observed.get(point["id"])
        if prediction is None or prediction not in matrix:
            continue
        reference = point["reference_class"]
        if reference not in matrix:
            continue
        matrix[reference][prediction] += 1
        retained.append(point)
    total = len(retained)
    if total < 10 or len({p["group_id"] for p in retained}) < 5:
        raise HTTPException(422, "Minimal 10 piksel valid dan 5 grup spasial independen harus tersedia pada hasil evaluasi GEE.")
    diagonal = sum(matrix[c][c] for c in classes)
    recalls = {str(c): matrix[c][c] / sum(matrix[c].values()) if sum(matrix[c].values()) else None for c in classes}
    precisions = {str(c): matrix[c][c] / sum(matrix[a][c] for a in classes) if sum(matrix[a][c] for a in classes) else None for c in classes}
    valid_recalls = [v for v in recalls.values() if v is not None]
    actual_marginal = [sum(matrix[c].values()) for c in classes]
    predicted_marginal = [sum(matrix[a][c] for a in classes) for c in classes]
    expected = sum(a * p for a, p in zip(actual_marginal, predicted_marginal)) / (total * total)
    accuracy = diagonal / total
    kappa = (accuracy - expected) / (1 - expected) if expected < 1 else 1.0
    return {"sample_count": total, "group_count": len({p["group_id"] for p in retained}),
            "overall_accuracy": accuracy, "balanced_accuracy": sum(valid_recalls) / len(valid_recalls),
            "kappa": kappa, "producer_accuracy_by_class": recalls,
            "user_accuracy_by_class": precisions,
            "confusion_matrix": {str(k): {str(pk): v for pk, v in row.items()} for k, row in matrix.items()},
            "no_data_count": len(points) - total}


@router.post("/releases/{model_id}/evaluate", status_code=201,
             dependencies=[Depends(require_permission("model.write")), Depends(require_ee)])
def evaluate_gee_landcover_release(model_id: int, payload: LandcoverReleaseEvaluation,
                                   admin: AdminUser = Depends(require_permission("model.write")),
                                   db: Session = Depends(get_db)):
    model = db.execute(select(UploadedModel).where(UploadedModel.id == model_id)
                       .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if model is None or (model.metadata_json or {}).get("format") != "gee_config_release":
        raise HTTPException(404, "GEE land-cover release not found")
    if model.lifecycle_state not in {"experimental", "evaluated"}:
        raise HTTPException(409, "Only an experimental release can be evaluated")
    config_path = gee_model_release_service.model_lifecycle.artifact_path(model)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    model_key, config = document["model_key"], document["config"]
    service.refresh_model_catalog()
    supported_years = config.get("supported_years") or service.LAND_COVER_DATASET_OPTIONS.get(model_key, {}).get("supported_years", [])
    if payload.year not in supported_years:
        raise HTTPException(422, f"Tahun {payload.year} tidak tercantum dalam periode model yang didukung.")
    if not payload.dataset_name.strip() or any(point.group_id != point.group_id.strip() for point in payload.points):
        raise HTTPException(422, "Nama dataset dan ID grup holdout tidak boleh kosong atau memiliki spasi tepi.")
    point_ids = [point.id for point in payload.points]
    if len(point_ids) != len(set(point_ids)):
        raise HTTPException(422, "ID titik evaluasi harus unik.")
    training_groups = {value.strip() for value in payload.training_group_ids if value.strip()}
    evaluation_groups = {point.group_id for point in payload.points}
    if len(training_groups) != len(payload.training_group_ids) or len(training_groups) < 5:
        raise HTTPException(422, "Sertakan minimal lima grup training yang unik untuk membuktikan split spasial.")
    if training_groups & evaluation_groups:
        raise HTTPException(422, "Grup spasial data training dan holdout harus terpisah.")
    classes = sorted(int(code) for code in config["classes"])
    if any(point.reference_class not in classes for point in payload.points):
        raise HTTPException(422, "Kelas referensi holdout harus terdaftar pada skema model.")
    if not (config.get("classifier_asset") or config.get("annual_asset_template") or model_key in service.RF_KEYS):
        raise HTTPException(409, "Model ini tidak memiliki adapter evaluasi GEE yang dapat dihitung server.")
    import ee
    points_as_dict = [point.model_dump() for point in payload.points]
    collection = ee.FeatureCollection([ee.Feature(ee.Geometry.Point([p["longitude"], p["latitude"]]),
        {"observation_id": p["id"], "reference": p["reference_class"], "group_id": p["group_id"]}) for p in points_as_dict])
    region = collection.geometry().bounds(maxError=100)
    features = service.feature_image(model_key, payload.year, region)
    if config.get("annual_asset_template"):
        source = config["annual_asset_template"].format(year=payload.year)
        prediction_image = ee.Image(source).select(config.get("class_band", "landcover")).rename("prediction")
        if config.get("class_encoding", "indices") == "indices":
            prediction_image = prediction_image.remap(list(range(len(classes))), classes).rename("prediction")
    else:
        classifier = (ee.Classifier.load(config["classifier_asset"]) if config.get("classifier_asset")
                      else service.build_reference_classifier(model_key, config, region)[0])
        prediction_image = features.classify(classifier).rename("prediction")
        if config.get("class_encoding", "indices") == "indices":
            prediction_image = prediction_image.remap(list(range(len(classes))), classes).rename("prediction")
        source = config.get("classifier_asset") or "server-rebuilt-reference-classifier"
    result = prediction_image.sampleRegions(collection=collection, properties=["observation_id", "reference", "group_id"],
        scale=int(config.get("native_scale", 10)), tileScale=8, geometries=False).getInfo()
    observed = {str(item["properties"].get("observation_id")): int(item["properties"]["prediction"])
                for item in result.get("features", []) if isinstance(item.get("properties"), dict)
                and item["properties"].get("prediction") is not None}
    metrics = _landcover_metrics(points_as_dict, observed, classes)
    dataset_raw = json.dumps({"points": points_as_dict, "training_group_ids": sorted(training_groups)},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    manifest = {"evaluation_kind": "gee_server_side_landcover_holdout", "model_key": model_key,
        "model_version": model.version, "model_sha256": model.artifact_sha256,
        "year": payload.year, "split_method": payload.split_method,
        "training_group_count": len(training_groups), "holdout_group_count": metrics["group_count"],
        "prediction_source": source, "class_schema": config.get("class_schema"),
        "dataset_source": payload.dataset_source,
        "provenance_status": "predictions_server_computed; dataset_source_and_group_split_operator_declared"}
    evaluation = gee_model_release_service.save_gee_evaluation(db, model, admin.id,
        dataset_name=payload.dataset_name, dataset_source=payload.dataset_source, dataset_raw=dataset_raw,
        manifest=manifest, metrics=metrics)
    db.add(AuditLog(admin_user_id=admin.id, action="model.evaluate.gee_landcover", resource_type="uploaded_model",
        resource_id=str(model.id), detail={"evaluation_id": evaluation.id, "dataset_sha256": evaluation.dataset_sha256,
                                           "metrics": metrics}))
    db.commit()
    return {"evaluation_id": evaluation.id, "artifact_sha256": model.artifact_sha256,
            "dataset_sha256": evaluation.dataset_sha256, "manifest": manifest, "metrics": metrics,
            "dataset_retained_encrypted": True}


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
