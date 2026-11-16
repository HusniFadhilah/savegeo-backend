"""One guard for local calibration approval, promotion and activation.

This is an internal local-model review, not formal carbon-project verification.
Approval pins the artifact and validation inputs; changed inputs need new review.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.audit_log import AuditLog
from app.db.models.carbon_calibration import CarbonCalibrationDataset, CarbonCalibrationModel, CarbonCalibrationRun
from app.services.carbon_calibration_service import sha256_file


def manifest(model, run, dataset) -> dict:
    blockers = []
    if run is None or run.status != "complete":
        blockers.append("completed_run_required")
    metrics = run.metrics or {} if run else {}
    report = dataset.validation_report or {} if dataset else {}
    if dataset is None:
        blockers.append("source_dataset_missing")
    if metrics.get("status") != "computed":
        blockers.append("validation_metrics_not_computed")
    if int(metrics.get("sample_count", metrics.get("n_plots", 0))) < 3:
        blockers.append("minimum_plot_count_not_met")
    if report.get("spatial", {}).get("blocking"):
        blockers.append("spatial_validation_blocked")
    if model.target_pool == "aboveground_biomass_carbon" and any(
            item.get("audit", {}).get("tree_review_count", 0) for item in report.get("workbook_audits", [])):
        blockers.append("unresolved_or_reviewed_label_records")
    if run and (run.target_pool != model.target_pool or run.dataset_pk != model.dataset_pk):
        blockers.append("run_model_scope_mismatch")
    if run and run.created_by is None:
        blockers.append("creator_unrecorded")
    artifact = Path(model.artifact_path)
    if not artifact.is_file():
        blockers.append("artifact_missing")
    elif sha256_file(artifact)[0] != model.artifact_sha256:
        blockers.append("artifact_checksum_mismatch")
    if blockers:
        raise HTTPException(409, detail={"code": "promotion_guard", "blockers": sorted(set(blockers))})
    inputs = {"guard_version": "local_calibration_review_v1", "run_id": run.id, "target_pool": model.target_pool,
              "artifact_sha256": model.artifact_sha256, "metrics": metrics, "parameters": run.parameters,
              "dataset_manifest": dataset.manifest, "validation_report": report,
              "source_files": sorted((item.id, item.sha256) for item in dataset.files)}
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"guard_version": inputs["guard_version"], "validation_digest": fingerprint, "artifact_sha256": model.artifact_sha256,
            "scope": "local_only", "formal_verification": False}


def _resources(db: Session, model_id: int):
    model = db.get(CarbonCalibrationModel, model_id)
    if model is None:
        raise HTTPException(404, "Calibration model not found")
    run = db.get(CarbonCalibrationRun, model.run_id) if model.run_id else None
    dataset = db.get(CarbonCalibrationDataset, model.dataset_pk)
    return model, run, dataset


def _lock(db: Session, model_id: int, dataset_id: int):
    db.execute(select(CarbonCalibrationDataset).where(CarbonCalibrationDataset.id == dataset_id).with_for_update()).scalar_one()
    model = db.execute(select(CarbonCalibrationModel).where(CarbonCalibrationModel.id == model_id).with_for_update()
                       .execution_options(populate_existing=True)).scalar_one()
    return model


def approve(db: Session, model_id: int, actor_id: int) -> CarbonCalibrationModel:
    model, run, dataset = _resources(db, model_id)
    approval = manifest(model, run, dataset)
    if run.created_by == actor_id:
        raise HTTPException(409, detail={"code": "separation_of_duties", "message": "Penyetuju harus berbeda dari pembuat run."})
    model = _lock(db, model.id, model.dataset_pk)
    db.refresh(run)
    db.refresh(dataset)
    if approval != manifest(model, run, dataset):
        raise HTTPException(409, "Validation inputs changed during review; repeat review")
    if model.status not in {"experimental", "approved_local"}:
        raise HTTPException(409, "Only experimental or inactive approved models can be reviewed")
    model.status, model.approved_by = "approved_local", actor_id
    model.metadata_json = {**(model.metadata_json or {}), "approval_manifest": approval,
                            "approved_at": dt.datetime.now(dt.UTC).isoformat(), "approved_by_id": actor_id}
    db.add(AuditLog(admin_user_id=actor_id, action="calibration.model.approve", resource_type="carbon_calibration_model",
                    resource_id=str(model.id), detail={"run_id": run.id, **approval}))
    db.commit()
    return model


def activate(db: Session, model_id: int, actor_id: int) -> CarbonCalibrationModel:
    model, run, dataset = _resources(db, model_id)
    current = manifest(model, run, dataset)
    stored = (model.metadata_json or {}).get("approval_manifest")
    if stored != current or model.approved_by is None or model.approved_by == run.created_by:
        raise HTTPException(409, detail={"code": "approval_required", "message": "Artefak dan validasi terkini perlu ditinjau oleh penyetuju yang berbeda."})
    model = _lock(db, model.id, model.dataset_pk)
    db.refresh(run)
    db.refresh(dataset)
    current = manifest(model, run, dataset)
    if (model.metadata_json or {}).get("approval_manifest") != current or model.approved_by is None or model.approved_by == run.created_by:
        raise HTTPException(409, "Approval changed during activation; repeat review")
    if model.status not in {"approved_local", "active_local"}:
        raise HTTPException(409, "Model must be approved before activation")
    db.query(CarbonCalibrationModel).filter(CarbonCalibrationModel.dataset_pk == model.dataset_pk,
        CarbonCalibrationModel.target_pool == model.target_pool, CarbonCalibrationModel.id != model.id,
        CarbonCalibrationModel.is_active.is_(True)).update(
            {CarbonCalibrationModel.is_active: False, CarbonCalibrationModel.status: "approved_local"}, synchronize_session=False)
    model.is_active, model.status = True, "active_local"
    db.add(AuditLog(admin_user_id=actor_id, action="calibration.model.activate", resource_type="carbon_calibration_model",
                    resource_id=str(model.id), detail=current))
    db.commit()
    return model
