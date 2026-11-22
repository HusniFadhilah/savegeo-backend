"""Shared, transactional lifecycle for uploaded executable models.

Evaluation values are computed from the submitted holdout rows. Dataset provenance
is recorded as uploader-declared evidence and is never presented as independently
verified field validation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.audit_log import AuditLog
from app.db.models.uploaded_model import UploadedModel, UploadedModelEvaluation, UploadedModelReview
from app.services import model_evaluation_storage


def artifact_path(model: UploadedModel) -> Path:
    root = get_settings().model_path.resolve()
    path = Path(model.filepath)
    if path.is_symlink():
        raise HTTPException(409, "Model artifact is not available for evaluation")
    resolved = path.resolve()
    metadata = model.metadata_json or {}
    artifact_format = metadata.get("format")
    supported = (
        artifact_format == "onnx" and resolved.suffix.lower() == ".onnx"
    ) or (
        artifact_format == "gee_saved_ensemble"
        and metadata.get("method") == "saved_reference_ensemble"
        and resolved.name == metadata.get("frozen_ensemble_file")
        and resolved.name.endswith(".ensemble.json")
    ) or (
        artifact_format == "gee_config_release"
        and metadata.get("method") == "gee_classifier_release"
        and resolved.suffix.lower() == ".json"
        and bool(metadata.get("model_key"))
    )
    if not resolved.is_relative_to(root) or not resolved.is_file() or not supported:
        raise HTTPException(409, "Model artifact is not available for evaluation")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if not model.artifact_sha256 or digest != model.artifact_sha256:
        raise HTTPException(409, "Model artifact changed; upload a new immutable version")
    return resolved


def approval_digest(model: UploadedModel, evaluation: UploadedModelEvaluation) -> str:
    payload = {
        "model_id": model.id,
        "artifact_sha256": model.artifact_sha256,
        "evaluation_id": evaluation.id,
        "dataset_sha256": evaluation.dataset_sha256,
        "manifest": evaluation.manifest,
        "metrics": evaluation.metrics,
        "policy": "uploaded_model_review_v1",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def latest_evaluation(db: Session, model: UploadedModel) -> UploadedModelEvaluation:
    evaluation = db.execute(
        select(UploadedModelEvaluation)
        .where(UploadedModelEvaluation.model_id == model.id)
        .order_by(UploadedModelEvaluation.created_at.desc(), UploadedModelEvaluation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if evaluation is None or evaluation.artifact_sha256 != model.artifact_sha256:
        raise HTTPException(409, "A server-computed evaluation for this exact model version is required")
    artifact_path(model)
    try:
        if not model_evaluation_storage.verify(evaluation.id, evaluation.dataset_sha256, evaluation.dataset_storage_path):
            raise HTTPException(409, "Encrypted evaluation dataset is missing or does not match its checksum")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - absent/rotated encryption key blocks approval
        raise HTTPException(503, "Encrypted evaluation dataset could not be verified") from exc
    return evaluation


def has_current_approval(db: Session, model: UploadedModel, evaluation: UploadedModelEvaluation) -> bool:
    review_record = db.execute(
        select(UploadedModelReview)
        .where(UploadedModelReview.model_id == model.id,
               UploadedModelReview.artifact_sha256 == model.artifact_sha256,
               UploadedModelReview.evaluation_id == evaluation.id,
               UploadedModelReview.decision == "approved")
        .order_by(UploadedModelReview.created_at.desc(), UploadedModelReview.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return bool(
        review_record
        and review_record.reviewer_id == model.approved_by
        and model.approved_by is not None
        and model.approved_by != model.uploaded_by
        and model.approval_digest == approval_digest(model, evaluation)
    )


def request_review(db: Session, model: UploadedModel, actor_id: int) -> None:
    locked = db.execute(select(UploadedModel).where(UploadedModel.id == model.id).with_for_update()
                        .execution_options(populate_existing=True)).scalar_one_or_none()
    if locked is None:
        raise HTTPException(404, "Model not found")
    if locked.lifecycle_state != "evaluated":
        raise HTTPException(409, "Only an evaluated model can be submitted for review")
    latest_evaluation(db, locked)
    locked.lifecycle_state = "review_pending"
    db.add(AuditLog(admin_user_id=actor_id, action="model.review.request", resource_type="uploaded_model",
                    resource_id=str(locked.id), detail={"artifact_sha256": locked.artifact_sha256}))
    db.commit()


def review(db: Session, model: UploadedModel, actor_id: int, decision: str, reason: str) -> UploadedModel:
    locked = db.execute(
        select(UploadedModel).where(UploadedModel.id == model.id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if locked is None:
        raise HTTPException(404, "Model not found")
    if locked.lifecycle_state != "review_pending":
        raise HTTPException(409, "Model is not awaiting review")
    evaluation = latest_evaluation(db, locked)
    if actor_id in {locked.uploaded_by, evaluation.evaluated_by}:
        raise HTTPException(409, detail={"code": "separation_of_duties", "message": "Reviewer must be different from uploader and evaluator"})
    if decision not in {"approved", "rejected"} or len(reason.strip()) < 10:
        raise HTTPException(422, "Choose approve/reject and provide a review note of at least 10 characters")

    now = dt.datetime.now(dt.UTC)
    digest = approval_digest(locked, evaluation)
    db.add(UploadedModelReview(model_id=locked.id, evaluation_id=evaluation.id,
        artifact_sha256=locked.artifact_sha256, decision=decision, reason=reason.strip(), reviewer_id=actor_id))
    locked.approved_by = actor_id if decision == "approved" else None
    locked.approved_at = now if decision == "approved" else None
    locked.approval_digest = digest if decision == "approved" else None
    locked.lifecycle_state = decision
    locked.is_active = False
    locked.is_default = False
    db.add(AuditLog(admin_user_id=actor_id, action=f"model.review.{decision}", resource_type="uploaded_model",
        resource_id=str(locked.id), detail={"evaluation_id": evaluation.id, "artifact_sha256": locked.artifact_sha256,
                                            "approval_digest": digest, "reason": reason.strip()}))
    db.commit()
    db.refresh(locked)
    return locked


def _lock_model_type(db: Session, model_type: str, scope: str | None = None) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.blake2b(f"{model_type}:{scope or '*'}".encode(), digest_size=8).digest(), "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def activate(db: Session, model: UploadedModel, actor_id: int) -> UploadedModel:
    model_metadata = model.metadata_json or {}
    scope = str(model_metadata.get("model_key")) if model_metadata.get("format") == "gee_config_release" else None
    _lock_model_type(db, model.model_type, scope)
    locked = db.execute(select(UploadedModel).where(UploadedModel.id == model.id).with_for_update()
                        .execution_options(populate_existing=True)).scalar_one_or_none()
    if locked is None:
        raise HTTPException(404, "Model not found")
    if locked.lifecycle_state != "approved" or locked.approved_by is None or locked.approved_by == locked.uploaded_by:
        raise HTTPException(409, "Independent approval of the current model version is required")
    evaluation = latest_evaluation(db, locked)
    if not has_current_approval(db, locked, evaluation):
        raise HTTPException(409, "Review evidence no longer matches this model version")
    if actor_id == locked.uploaded_by:
        raise HTTPException(409, detail={"code": "separation_of_duties", "message": "Uploader cannot activate their own model"})

    siblings = db.execute(select(UploadedModel).where(UploadedModel.model_type == locked.model_type).with_for_update()).scalars().all()
    for sibling in siblings:
        sibling_metadata = sibling.metadata_json or {}
        sibling_scope = str(sibling_metadata.get("model_key")) if sibling_metadata.get("format") == "gee_config_release" else None
        if sibling.id != locked.id and sibling.lifecycle_state == "active" and sibling_scope == scope:
            sibling.lifecycle_state, sibling.is_active, sibling.is_default = "approved", False, False
    locked.lifecycle_state, locked.is_active = "active", True
    locked.is_default = False
    db.add(AuditLog(admin_user_id=actor_id, action="model.activate", resource_type="uploaded_model",
        resource_id=str(locked.id), detail={"artifact_sha256": locked.artifact_sha256,
                                            "evaluation_id": evaluation.id, "approval_digest": locked.approval_digest}))
    db.commit()
    db.refresh(locked)
    return locked


def set_default(db: Session, model: UploadedModel, actor_id: int) -> UploadedModel:
    _lock_model_type(db, model.model_type)
    locked = db.execute(select(UploadedModel).where(UploadedModel.id == model.id).with_for_update()
                        .execution_options(populate_existing=True)).scalar_one_or_none()
    if locked is None:
        raise HTTPException(404, "Model not found")
    if locked.lifecycle_state != "active" or not locked.is_active:
        raise HTTPException(409, "Only an approved active model can become the default")
    if (locked.metadata_json or {}).get("format") != "onnx":
        raise HTTPException(409, "Only general ONNX models can be selected as the registry default; pool-specific Earth Engine models are selected by their pool adapter.")
    evaluation = latest_evaluation(db, locked)
    if not has_current_approval(db, locked, evaluation):
        raise HTTPException(409, "Review evidence no longer matches this model version")
    db.execute(select(UploadedModel).where(UploadedModel.model_type == locked.model_type).with_for_update()).scalars().all()
    db.query(UploadedModel).filter(UploadedModel.model_type == locked.model_type).update(
        {UploadedModel.is_default: False}, synchronize_session=False)
    locked.is_default = True
    db.add(AuditLog(admin_user_id=actor_id, action="model.default", resource_type="uploaded_model",
        resource_id=str(locked.id), detail={"artifact_sha256": locked.artifact_sha256,
                                            "approval_digest": locked.approval_digest}))
    db.commit()
    db.refresh(locked)
    return locked


def archive(db: Session, model: UploadedModel, actor_id: int) -> None:
    _lock_model_type(db, model.model_type)
    locked = db.execute(select(UploadedModel).where(UploadedModel.id == model.id).with_for_update()
                        .execution_options(populate_existing=True)).scalar_one_or_none()
    if locked is None:
        raise HTTPException(404, "Model not found")
    locked.lifecycle_state, locked.is_active, locked.is_default = "archived", False, False
    db.add(AuditLog(admin_user_id=actor_id, action="model.archive", resource_type="uploaded_model",
        resource_id=str(locked.id), detail={"artifact_sha256": locked.artifact_sha256}))
    db.commit()
