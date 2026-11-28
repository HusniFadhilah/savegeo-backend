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
from app.db.models.carbon_calibration import (
    CarbonCalibrationDataset,
    CarbonCalibrationModel,
    CarbonCalibrationRun,
    CarbonFieldObservation,
)
from app.db.models.uploaded_model import UploadedModel
from app.services.carbon_calibration_service import sha256_file


def _field_prediction_evidence(db: Session | None, run, dataset, baseline: dict) -> list[str]:
    blockers: list[str] = []
    if db is None:
        return ["field_evidence_database_required"]
    try:
        model_id = int(baseline.get("database_id"))
    except (TypeError, ValueError):
        return ["baseline_model_identity_missing"]
    base = db.get(UploadedModel, model_id)
    if (
        base is None
        or base.name != baseline.get("id")
        or base.version != baseline.get("version")
        or base.artifact_sha256 != baseline.get("artifact_sha256")
        or base.lifecycle_state != "active"
        or not base.is_active
        or (base.metadata_json or {}).get("preprocessing_version") != baseline.get("preprocessing_version")
    ):
        blockers.append("baseline_model_identity_mismatch")
    else:
        try:
            from app.services import model_lifecycle

            evaluation = model_lifecycle.latest_evaluation(db, base)
            if not model_lifecycle.has_current_approval(db, base, evaluation):
                blockers.append("baseline_model_approval_stale")
            model_lifecycle.artifact_path(base)
        except Exception:
            blockers.append("baseline_model_artifact_unverified")

    provenance = (run.metrics or {}).get("provenance") or {}
    observation_ids = provenance.get("observation_ids") if isinstance(provenance, dict) else None
    if not isinstance(observation_ids, list) or not observation_ids:
        return [*blockers, "field_observation_snapshot_missing"]
    observations = db.query(CarbonFieldObservation).filter(
        CarbonFieldObservation.dataset_pk == dataset.id,
        CarbonFieldObservation.id.in_(observation_ids),
    ).order_by(CarbonFieldObservation.id).all()
    if len(observations) != len(set(observation_ids)):
        blockers.append("field_observation_snapshot_changed")
        return blockers
    observed_digest = hashlib.sha256(
        json.dumps([item.to_dict() for item in observations], sort_keys=True).encode()
    ).hexdigest()
    if observed_digest != provenance.get("snapshot_sha256"):
        blockers.append("field_observation_snapshot_changed")

    source = (run.parameters or {}).get("prediction_source")
    training_ids = set(map(str, (run.metrics or {}).get("training_plot_ids", [])))
    validation_ids = set(map(str, (run.metrics or {}).get("validation_plot_ids", [])))
    used_plot_ids = training_ids | validation_ids
    rows_by_id = {item.id: item for item in observations}
    for plot_ids, role in ((training_ids, "calibration"), (validation_ids, "validation")):
        plot_ids_in_source = {
            str((rows_by_id[item_id].raw or {}).get("plot_id"))
            for item_id in rows_by_id if rows_by_id[item_id].role == role
        }
        if not plot_ids.issubset(plot_ids_in_source):
            blockers.append("field_plot_split_changed")
    for observation in observations:
        if str((observation.raw or {}).get("plot_id")) not in used_plot_ids:
            continue
        prediction = next((item for item in (observation.predictions or []) if item.get("source") == source), None)
        if prediction is None:
            blockers.append("field_prediction_source_missing")
            break

    # Require the selected predictions to be present in a completed server-side
    # extraction snapshot pinned to this exact approved model artifact.
    prediction_runs = db.query(CarbonCalibrationRun).filter_by(
        dataset_pk=dataset.id, method="gee_field_prediction", status="complete"
    ).all()
    covered: set[str] = set()
    for prediction_run in prediction_runs:
        parameters = prediction_run.parameters or {}
        metrics = prediction_run.metrics or {}
        if (
            parameters.get("prediction_source") != source
            or parameters.get("model_id") != model_id
            or parameters.get("model_version") != baseline.get("version")
            or parameters.get("artifact_sha256") != baseline.get("artifact_sha256")
        ):
            continue
        batch_ids = parameters.get("observation_ids") or []
        batch_rows = [rows_by_id[item_id] for item_id in batch_ids if item_id in rows_by_id]
        snapshot_rows = []
        for item in batch_rows:
            prediction = next((candidate for candidate in (item.predictions or []) if candidate.get("source") == source), None)
            if prediction:
                snapshot_rows.append({"observation_id": item.id, **prediction})
        snapshot_digest = hashlib.sha256(
            json.dumps(snapshot_rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        if snapshot_digest == metrics.get("prediction_snapshot_sha256"):
            covered.update(batch_ids)
    required_observation_ids = {item.id for item in observations if str((item.raw or {}).get("plot_id")) in used_plot_ids}
    if not required_observation_ids.issubset(covered):
        blockers.append("field_prediction_execution_evidence_missing")
    return blockers


def manifest(model, run, dataset, db: Session | None = None) -> dict:
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
    parameters = run.parameters or {} if run else {}
    baseline = parameters.get("baseline_model") if isinstance(parameters.get("baseline_model"), dict) else {}
    is_field_assessment = bool(run and run.method == "field_plot_calibration")
    if not all(baseline.get(key) for key in ("id", "version", "artifact_sha256", "preprocessing_version")):
        # Field assessments store the selected model identity beside its source.
        if not is_field_assessment:
            blockers.append("baseline_model_identity_missing")
    training_plot_ids = parameters.get("training_plot_ids") if isinstance(parameters.get("training_plot_ids"), list) else []
    validation_plot_ids = parameters.get("validation_plot_ids") if isinstance(parameters.get("validation_plot_ids"), list) else []
    known_plot_ids = {plot.plot_id for plot in dataset.plots} if dataset else set()
    if (
        len(set(map(str, training_plot_ids))) < 3
        or len(set(map(str, validation_plot_ids))) < 3
        or set(map(str, training_plot_ids)) & set(map(str, validation_plot_ids))
        or (not is_field_assessment and not set(map(str, training_plot_ids + validation_plot_ids)).issubset(known_plot_ids))
        or (metrics.get("validation_type") != "independent_field_plot_holdout" and not is_field_assessment)
        or (not is_field_assessment and not isinstance(metrics.get("independent_validation_metrics"), dict))
        or (not is_field_assessment and metrics["independent_validation_metrics"].get("status") != "computed")
        or int((metrics.get("independent_validation_metrics") or {}).get("n_plots", (metrics.get("independent_validation_metrics") or {}).get("sample_count", 0))) < 3
    ):
        blockers.append("independent_validation_split_missing")
    domain = (dataset.manifest or {}).get("application_domain_geojson") if dataset else None
    try:
        from shapely.geometry import shape

        domain_geometry = shape(domain) if isinstance(domain, dict) else None
        domain_valid = bool(domain_geometry and not domain_geometry.is_empty and domain_geometry.is_valid and domain_geometry.geom_type in {"Polygon", "MultiPolygon"})
    except Exception:  # malformed persisted application scope fails closed
        domain_valid = False
    if not domain_valid:
        blockers.append("calibration_application_domain_missing")
    if is_field_assessment:
        if metrics.get("validation_method") != "leave_one_plot_out_and_separate_validation":
            blockers.append("independent_validation_split_missing")
        if int(metrics.get("calibration_plots", 0)) < 3 or int(metrics.get("validation_plots", 0)) < 3:
            blockers.append("independent_validation_split_missing")
        if not isinstance(metrics.get("calibrated_validation"), dict) or metrics["calibrated_validation"].get("status") != "computed":
            blockers.append("independent_validation_split_missing")
        blockers.extend(_field_prediction_evidence(db, run, dataset, baseline))
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
    inputs = {"guard_version": "local_calibration_review_v2", "run_id": run.id, "target_pool": model.target_pool,
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


def _history(model: CarbonCalibrationModel) -> list[dict]:
    value = (model.metadata_json or {}).get("activation_history", [])
    return list(value) if isinstance(value, list) else []


def approve(
    db: Session,
    model_id: int,
    actor_id: int,
    *,
    reason: str | None = None,
    reviewer: dict | None = None,
) -> CarbonCalibrationModel:
    model, run, dataset = _resources(db, model_id)
    if run is not None and run.created_by == actor_id:
        raise HTTPException(409, detail={"code": "separation_of_duties", "message": "Penyetuju harus berbeda dari pembuat run."})
    approval = manifest(model, run, dataset, db)
    model = _lock(db, model.id, model.dataset_pk)
    db.refresh(run)
    db.refresh(dataset)
    if approval != manifest(model, run, dataset, db):
        raise HTTPException(409, "Validation inputs changed during review; repeat review")
    if model.status not in {"experimental", "pending_approval", "approved_local", "rolled_back"}:
        raise HTTPException(409, "Only experimental or inactive approved models can be reviewed")
    model.status, model.approved_by = "approved_local", actor_id
    model.metadata_json = {**(model.metadata_json or {}), "approval_manifest": approval,
                            "review": {
                                "decision": "approved",
                                "reviewed_at": dt.datetime.now(dt.UTC).isoformat(),
                                "reviewed_by_id": actor_id,
                                "reviewer": reviewer or {},
                                "reason": (reason or "").strip()[:1000] or "not_provided",
                            },
                            "approved_at": dt.datetime.now(dt.UTC).isoformat(), "approved_by_id": actor_id}
    db.add(AuditLog(admin_user_id=actor_id, action="calibration.model.approve", resource_type="carbon_calibration_model",
                    resource_id=str(model.id), detail={"run_id": run.id, "reason": (reason or "").strip()[:1000] or "not_provided", **approval}))
    db.commit()
    return model


def reject(db: Session, model_id: int, actor_id: int, *, reason: str, reviewer: dict | None = None) -> CarbonCalibrationModel:
    model, run, dataset = _resources(db, model_id)
    if run is not None and run.created_by == actor_id:
        raise HTTPException(409, detail={"code": "separation_of_duties", "message": "Reviewer harus berbeda dari pembuat run."})
    if not reason.strip():
        raise HTTPException(422, "Rejection reason is required")
    manifest(model, run, dataset, db)
    model = _lock(db, model.id, model.dataset_pk)
    db.refresh(run)
    db.refresh(dataset)
    approval = manifest(model, run, dataset, db)
    if model.is_active or model.status not in {"experimental", "pending_approval", "approved_local", "rolled_back"}:
        raise HTTPException(409, "Only inactive reviewable calibration models can be rejected")
    now = dt.datetime.now(dt.UTC).isoformat()
    model.is_active, model.status, model.approved_by = False, "rejected", actor_id
    model.metadata_json = {
        **(model.metadata_json or {}),
        "review": {"decision": "rejected", "reviewed_at": now, "reviewed_by_id": actor_id,
                   "reviewer": reviewer or {}, "reason": reason.strip()[:1000]},
    }
    db.add(AuditLog(admin_user_id=actor_id, action="calibration.model.reject", resource_type="carbon_calibration_model",
                    resource_id=str(model.id), detail={"reason": reason.strip()[:1000], **approval}))
    db.commit()
    return model


def activate(db: Session, model_id: int, actor_id: int) -> CarbonCalibrationModel:
    model, run, dataset = _resources(db, model_id)
    current = manifest(model, run, dataset, db)
    stored = (model.metadata_json or {}).get("approval_manifest")
    if stored != current or model.approved_by is None or model.approved_by == run.created_by:
        raise HTTPException(409, detail={"code": "approval_required", "message": "Artefak dan validasi terkini perlu ditinjau oleh penyetuju yang berbeda."})
    model = _lock(db, model.id, model.dataset_pk)
    db.refresh(run)
    db.refresh(dataset)
    current = manifest(model, run, dataset, db)
    if (model.metadata_json or {}).get("approval_manifest") != current or model.approved_by is None or model.approved_by == run.created_by:
        raise HTTPException(409, "Approval changed during activation; repeat review")
    if model.status not in {"approved_local", "active_local", "rolled_back"}:
        raise HTTPException(409, "Model must be approved before activation")
    prior_models = db.query(CarbonCalibrationModel).filter(
        CarbonCalibrationModel.dataset_pk == model.dataset_pk,
        CarbonCalibrationModel.target_pool == model.target_pool,
        CarbonCalibrationModel.id != model.id,
        CarbonCalibrationModel.is_active.is_(True),
    ).with_for_update().all()
    now = dt.datetime.now(dt.UTC).isoformat()
    for prior in prior_models:
        metadata = dict(prior.metadata_json or {})
        prior.is_active, prior.status = False, "approved_local"
        prior.metadata_json = {
            **metadata,
            "replaced_by_model_id": model.id,
            "activation_history": [*_history(prior), {"event": "replaced", "at": now, "by_model_id": model.id, "actor_id": actor_id}],
        }
    model_metadata = dict(model.metadata_json or {})
    model.is_active, model.status = True, "active_local"
    model.metadata_json = {
        **model_metadata,
        "activated_at": now,
        "activation_history": [*_history(model), {"event": "activated", "at": now, "actor_id": actor_id}],
    }
    db.add(AuditLog(admin_user_id=actor_id, action="calibration.model.activate", resource_type="carbon_calibration_model",
                    resource_id=str(model.id), detail={"prior_model_ids": [item.id for item in prior_models], **current}))
    db.commit()
    return model


def rollback(db: Session, model_id: int, actor_id: int, *, reason: str | None = None) -> tuple[CarbonCalibrationModel, CarbonCalibrationModel]:
    """Deactivate the selected active version and restore its latest approved predecessor."""
    model, run, dataset = _resources(db, model_id)
    if not model.is_active or model.status != "active_local":
        raise HTTPException(409, "Rollback requires the currently active calibration model")
    model = _lock(db, model.id, model.dataset_pk)
    current_activation = (model.metadata_json or {}).get("activated_at")
    if not current_activation:
        raise HTTPException(409, detail={"code": "rollback_history_missing", "message": "Active model has no recorded activation history."})
    candidates = db.query(CarbonCalibrationModel).filter(
        CarbonCalibrationModel.dataset_pk == model.dataset_pk,
        CarbonCalibrationModel.target_pool == model.target_pool,
        CarbonCalibrationModel.id != model.id,
        CarbonCalibrationModel.is_active.is_(False),
        CarbonCalibrationModel.status == "approved_local",
    ).with_for_update().all()
    eligible: list[tuple[str, CarbonCalibrationModel]] = []
    for candidate in candidates:
        history = [entry for entry in _history(candidate) if entry.get("event") in {"activated", "replaced"}]
        prior_activation = next((entry.get("at") for entry in reversed(history) if entry.get("at")), None)
        candidate_run = db.get(CarbonCalibrationRun, candidate.run_id) if candidate.run_id else None
        candidate_dataset = db.get(CarbonCalibrationDataset, candidate.dataset_pk)
        if not prior_activation or prior_activation > current_activation or candidate_run is None or candidate_dataset is None:
            continue
        try:
            approval_is_current = (candidate.metadata_json or {}).get("approval_manifest") == manifest(
                candidate, candidate_run, candidate_dataset, db
            )
        except HTTPException:
            approval_is_current = False
        if (
            approval_is_current
            and Path(candidate.artifact_path).is_file()
            and sha256_file(Path(candidate.artifact_path))[0] == candidate.artifact_sha256
        ):
            eligible.append((prior_activation, candidate))
    if not eligible:
        raise HTTPException(409, detail={"code": "rollback_target_missing", "message": "No prior approved, checksum-valid calibration version is available."})
    prior_activation, restored = max(eligible, key=lambda item: item[0])
    now = dt.datetime.now(dt.UTC).isoformat()
    reason_text = (reason or "").strip()[:1000] or "not_provided"
    current_metadata = dict(model.metadata_json or {})
    model.is_active, model.status = False, "rolled_back"
    model.metadata_json = {
        **current_metadata,
        "rollback_at": now,
        "rollback_reason": reason_text,
        "rollback_restored_model_id": restored.id,
        "activation_history": [*_history(model), {"event": "rolled_back", "at": now, "restored_model_id": restored.id, "actor_id": actor_id, "reason": reason_text}],
    }
    restored_metadata = dict(restored.metadata_json or {})
    restored.is_active, restored.status = True, "active_local"
    restored.metadata_json = {
        **restored_metadata,
        "activated_at": now,
        "restored_from_model_id": model.id,
        "activation_history": [*_history(restored), {"event": "rollback_restored", "at": now, "from_model_id": model.id, "actor_id": actor_id}],
    }
    db.add(AuditLog(admin_user_id=actor_id, action="calibration.model.rollback", resource_type="carbon_calibration_model",
                    resource_id=str(model.id), detail={"restored_model_id": restored.id, "previous_activation_at": prior_activation, "reason": reason_text}))
    db.commit()
    return model, restored
