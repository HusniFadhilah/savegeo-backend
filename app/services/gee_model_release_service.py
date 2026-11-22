"""Immutable GEE model configuration artifacts, sharing uploaded-model review."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel, UploadedModelEvaluation
from app.services import model_lifecycle, model_evaluation_storage


def _root() -> Path:
    model_root = get_settings().model_path.resolve()
    root = model_root / "gee-releases"
    if root.is_symlink():
        raise ValueError("GEE release directory is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if not root.is_relative_to(model_root):
        raise ValueError("GEE release directory is unsafe")
    return root


def register_landcover_config(db: Session, admin_id: int, model_key: str, config: dict) -> UploadedModel:
    document = {"schema": "gee-landcover-release-v1", "model_key": model_key, "config": config}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    name = f"gee-lulc-{model_key.lower()}-{digest[:16]}"
    existing = db.query(UploadedModel).filter_by(name=name).one_or_none()
    if existing:
        return existing
    path = _root() / f"{name}.json"
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".release-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    row = UploadedModel(
        name=name, display_name=f"{model_key} · {config.get('model_version') or digest[:12]}",
        model_type="landcover", algorithm="Earth Engine classifier", filename=path.name,
        filepath=str(path.resolve()), file_size_kb=round(len(raw) / 1024, 2), is_default=False,
        is_active=False, lifecycle_state="experimental", is_legacy=False,
        description="Konfigurasi classifier GEE immutable; menunggu evaluasi independen dan review.",
        version=str(config.get("model_version") or digest[:16]), metrics=None, feature_names=None,
        metadata_json={"format": "gee_config_release", "method": "gee_classifier_release",
                      "model_key": model_key, "config_sha256": digest},
        artifact_sha256=digest, uploaded_by=admin_id,
    )
    db.add(row)
    db.flush()
    return row


def active_landcover_config(model_key: str) -> tuple[dict, dict] | None:
    """Return the reviewed immutable config currently selected for this key."""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        rows = db.query(UploadedModel).filter_by(
            model_type="landcover", lifecycle_state="active", is_active=True
        ).all()
        model = next((row for row in rows if (row.metadata_json or {}).get("model_key") == model_key
                      and (row.metadata_json or {}).get("format") == "gee_config_release"), None)
        if model is None:
            return None
        evaluation = model_lifecycle.latest_evaluation(db, model)
        if not model_lifecycle.has_current_approval(db, model, evaluation):
            return None
        path = model_lifecycle.artifact_path(model)
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema") != "gee-landcover-release-v1" or document.get("model_key") != model_key:
            return None
        return document["config"], {"id": model.id, "name": model.name, "version": model.version,
                                    "artifact_sha256": model.artifact_sha256,
                                    "approval_digest": model.approval_digest}


def pin_active_landcover_release(db: Session, model_key: str) -> dict | None:
    rows = db.query(UploadedModel).filter_by(
        model_type="landcover", lifecycle_state="active", is_active=True
    ).all()
    model = next((row for row in rows if (row.metadata_json or {}).get("model_key") == model_key
                  and (row.metadata_json or {}).get("format") == "gee_config_release"), None)
    if model is None:
        return None
    evaluation = model_lifecycle.latest_evaluation(db, model)
    if not model_lifecycle.has_current_approval(db, model, evaluation):
        return None
    model_lifecycle.artifact_path(model)
    return {"id": model.id, "name": model.name, "version": model.version,
            "artifact_sha256": model.artifact_sha256, "approval_digest": model.approval_digest,
            "format": "gee_config_release", "model_key": model_key}


def save_gee_evaluation(db: Session, model: UploadedModel, admin_id: int, *, dataset_name: str,
                        dataset_source: str | None, dataset_raw: bytes, manifest: dict, metrics: dict) -> UploadedModelEvaluation:
    if model.lifecycle_state not in {"experimental", "evaluated"}:
        from fastapi import HTTPException
        raise HTTPException(409, "This model version is no longer editable or evaluable")
    from fastapi import HTTPException
    evaluation = UploadedModelEvaluation(
        model_id=model.id, artifact_sha256=model.artifact_sha256, dataset_name=dataset_name,
        dataset_sha256=hashlib.sha256(dataset_raw).hexdigest(), dataset_source=dataset_source,
        manifest=manifest, metrics=metrics, evaluated_by=admin_id,
    )
    db.add(evaluation)
    db.flush()
    try:
        evaluation.dataset_storage_path = model_evaluation_storage.store(evaluation.id, evaluation.dataset_sha256, dataset_raw)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(503, "Encrypted evaluation retention is not configured") from exc
    model.lifecycle_state = "evaluated"
    model.approved_by = model.approved_at = model.approval_digest = None
    return evaluation
