"""Adapter from the durable queue to existing scientific application services."""
from __future__ import annotations

import json
import uuid

import requests
from fastapi import HTTPException
from sqlalchemy import select

from app.core.viewer_features import ensure_job_feature_access
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.user import User
from app.db.models.workspace_analysis import WorkspaceAnalysisRun
from app.services.dmrv.authz import authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.job_handlers import JobContext, JobFailed, RetryableJobError
from app.services.workspace_analysis import digest
from app.services.analysis_requirements import requires_earth_engine


def validate(payload: dict) -> dict:
    if set(payload) != {"context"} or not isinstance(payload["context"], dict):
        raise ValueError("Server-resolved project context is required")
    return payload


def execute(db, kind: str, parameters: dict) -> dict:
    # These are the same services used by exploration, not a second algorithm.
    from app.services import carbon_service, landcover_service, vegetation_service
    if kind == "carbon":
        return carbon_service.analyze_carbon(db, parameters)
    if kind == "vegetation":
        return vegetation_service.analyze_vegetation(db, parameters)
    if kind == "landcover":
        return landcover_service.analyze_landcover(parameters)
    if kind == "landcover_transition":
        return landcover_service.analyze_landcover_transition(parameters)
    raise JobFailed("unsupported_analysis", "Jenis analisis tidak tersedia.")


def _execute_with_credential_failover(ctx: JobContext, kind: str, parameters: dict) -> dict:
    """Retry once per verified key only for GEE authentication/permission errors.

    dMRV workers execute one job at a time per process, so reinitializing the
    process-global Earth Engine client here cannot change another job's identity.
    Quota and rate-limit failures are deliberately left to bounded backoff.
    """
    from app.services import gee_service
    from app.services.gee_common import AnalysisError

    tried_ids: set[int] = set()
    while True:
        try:
            return execute(ctx.db, kind, parameters)
        except AnalysisError as exc:
            credential_id = gee_service.runtime_credential_id()
            if (not gee_service.is_credential_auth_error(exc)
                    or credential_id is None or credential_id in tried_ids):
                raise
            tried_ids.add(credential_id)
            ctx.checkpoint()
            if not gee_service.initialize_ee(ctx.control, exclude_credential_ids=tried_ids):
                if gee_service.last_initialization_was_rate_limited():
                    raise RetryableJobError(
                        "provider_rate_limited",
                        "Earth Engine sedang membatasi permintaan. Pekerjaan akan dicoba lagi.",
                    ) from None
                raise


def _carbon_mrv_status(context: dict, outputs: dict) -> dict:
    provenance = context.get("carbon_provenance") if isinstance(context.get("carbon_provenance"), dict) else {}
    method = provenance.get("methodology") if isinstance(provenance.get("methodology"), dict) else {}
    sampling = provenance.get("sampling_plan") if isinstance(provenance.get("sampling_plan"), dict) else {}
    field = provenance.get("field_evidence") if isinstance(provenance.get("field_evidence"), dict) else {}
    estimate = outputs.get("carbon_estimated") if isinstance(outputs.get("carbon_estimated"), dict) else {}
    reference = outputs.get("carbon_reference") if isinstance(outputs.get("carbon_reference"), dict) else {}
    model = outputs.get("model_info") if isinstance(outputs.get("model_info"), dict) else {}
    quality = outputs.get("data_quality") if isinstance(outputs.get("data_quality"), dict) else {}
    calibration = model.get("calibration") if isinstance(model.get("calibration"), dict) else None
    calibration_pin = provenance.get("calibration") if isinstance(provenance.get("calibration"), dict) else None
    calibration_status = calibration.get("status") if calibration else ("not_applied" if calibration_pin else "not_requested")
    field_count = int(field.get("survey_record_rows") or 0) + int(field.get("field_capture_rows") or 0)
    source_year = model.get("analysis_year") or (context.get("parameters") or {}).get("year")
    pool = model.get("target_pool") or estimate.get("target_pool") or reference.get("target_pool")
    return {
        "schema": "savegeo-carbon-dmrv-v1",
        "project_id": context.get("project_id"),
        "monitoring_period_id": (context.get("period") or {}).get("id"),
        "estimate": {
            "status": "model_estimate",
            "pool": pool,
            "source_dataset": model.get("reference_dataset") or reference.get("name"),
            "source_year": source_year,
            "model_name": model.get("model_name"),
            "model_version": model.get("model_version"),
            "model_pin": provenance.get("model_pin"),
            "imagery_provenance": quality.get("source_provenance", []),
        },
        "calibration": {
            "status": calibration_status,
            "model_id": calibration.get("model_id") if calibration else (calibration_pin or {}).get("model_id"),
            "model_version": calibration.get("model_version") if calibration else (calibration_pin or {}).get("model_version"),
            "artifact_sha256": calibration.get("artifact_sha256") if calibration else (calibration_pin or {}).get("artifact_sha256"),
            "reason": calibration.get("reason") if calibration else ("calibration_result_not_recorded" if calibration_pin else None),
        },
        "validation": {
            "status": "not_validated",
            "validation_type": "none",
            "raster_reference": "comparison_available_not_independent_validation" if reference else "not_available",
            "field_plot_evidence": "linked_not_validated" if field_count else "not_available",
        },
        "segmentation": {
            "status": "not_linked",
            "review_status": "not_reviewed",
            "accepted_for_stratification": False,
            "blockers": ["project_bound_segmentation_not_linked", "class_schema_and_map_quality_review_required"],
        },
        "review": {"status": "not_reviewed", "workflow": "evidence_package_and_independent_project_review"},
        "accounting": {
            "status": "not_calculated",
            "result_type": "net_project_outcome",
            "methodology_id": method.get("methodology_id"),
            "methodology_version": method.get("methodology_version"),
            "blockers": method.get("blockers", ["methodology_specific_net_outcome_calculator_not_implemented"]),
        },
        "registry": {"status": "not_connected"},
        "provenance": {
            "boundary": context.get("boundary"),
            "period": context.get("period"),
            "baseline": context.get("baseline"),
            "sampling_plan": sampling,
            "field_evidence": field,
            "project_revision": context.get("project_revision"),
        },
    }


def run(ctx: JobContext) -> dict:
    from app.services import gee_service
    from app.services.gee_common import AnalysisError, public_analysis_error

    ctx.stage("authorize")
    run_record = ctx.db.get(WorkspaceAnalysisRun, ctx.job_id)
    if run_record is None or digest(run_record.context) != run_record.context_hash or ctx.payload != {"context": run_record.context}:
        raise JobFailed("context_mismatch", "Konteks pekerjaan tidak cocok dengan catatan server.")
    context = run_record.context
    kind = ctx.db.get(DmrvJob, ctx.job_id).kind.removeprefix("analysis.")
    parameters = context["parameters"]
    account_class = User if ctx.principal.type == "user" else AdminUser
    account = ctx.db.get(account_class, ctx.principal.id)
    try:
        if account is None or not account.is_active:
            raise JobFailed("account_inactive", "Akun pembuat pekerjaan tidak aktif.")
        authorize(ctx.db, ctx.project_id, ctx.principal, "analysis.run")
        ensure_job_feature_access(account, kind, ctx.db, parameters)
    except (DmrvError, HTTPException):
        raise JobFailed("access_revoked", "Izin menjalankan analisis sudah tidak tersedia.") from None
    if parameters.get("_server_model_pin"):
        try:
            from app.api.routes.analysis_jobs import _verify_job_model_pin

            _verify_job_model_pin(ctx.db, parameters)
        except AnalysisError as exc:
            raise JobFailed("model_version_changed", public_analysis_error(exc)) from None
    ctx.db.rollback()  # release read transaction before slow provider calls
    ctx.checkpoint()
    ctx.stage("initialize_provider")
    # initialize_ee may update credential last-used; use the control session,
    # never the transaction that will publish the immutable result.
    if requires_earth_engine(kind, parameters) and not gee_service.initialize_ee(ctx.control):
        raise RetryableJobError("provider_unavailable", "Earth Engine belum siap. Periksa kredensial layanan.")
    ctx.stage("analyze")
    try:
        result = _execute_with_credential_failover(ctx, kind, parameters)
    except AnalysisError as exc:
        error = RetryableJobError if exc.status_code in {429, 502, 503, 504} else JobFailed
        raise error("analysis_failed", public_analysis_error(exc)) from None
    except requests.RequestException:
        raise RetryableJobError("provider_connection", "Koneksi penyedia data terputus; pekerjaan akan dicoba ulang.") from None
    if kind == "carbon":
        provenance = context.get("carbon_provenance") if isinstance(context.get("carbon_provenance"), dict) else {}
        calibration_pin = provenance.get("calibration") if isinstance(provenance.get("calibration"), dict) else None
        result_model = result.get("model_info") if isinstance(result, dict) and isinstance(result.get("model_info"), dict) else {}
        calibration_result = result_model.get("calibration") if isinstance(result_model.get("calibration"), dict) else {}
        if calibration_pin and calibration_result.get("status") == "applied":
            expected_identity = (
                calibration_pin.get("model_id"),
                calibration_pin.get("model_version"),
                calibration_pin.get("artifact_sha256"),
            )
            applied_identity = (
                calibration_result.get("model_id"),
                calibration_result.get("model_version"),
                calibration_result.get("artifact_sha256"),
            )
            if not calibration_pin.get("model_id") or applied_identity != expected_identity:
                raise JobFailed(
                    "calibration_version_changed",
                    "Versi kalibrasi berubah setelah pekerjaan dibuat. Jalankan ulang agar snapshot memakai versi yang dipilih.",
                )
    ctx.checkpoint()
    ctx.stage("snapshot")
    # Resolve permissions again after inference: membership can change while a
    # provider request is running. No snapshot is staged until this check passes.
    ctx.db.expire_all()
    try:
        authorize(ctx.db, ctx.project_id, ctx.principal, "analysis.run")
        account = ctx.db.get(account_class, ctx.principal.id)
        if account is None or not account.is_active:
            raise JobFailed("account_inactive", "Akun pembuat pekerjaan tidak aktif.")
        ensure_job_feature_access(account, kind, ctx.db, parameters)
    except (DmrvError, HTTPException):
        raise JobFailed("access_revoked", "Izin analisis dicabut sebelum hasil diterbitkan.") from None
    outputs = json.loads(json.dumps(result, allow_nan=False))
    if kind == "carbon":
        outputs["carbon_mrv"] = _carbon_mrv_status(context, outputs)
    inputs = {"context": context, "context_hash": digest(context), "adapter_version": "project_analysis_v1"}
    snapshot_hash = digest({"kind": kind, "inputs": inputs, "outputs": outputs})
    snapshot = ctx.db.scalar(select(DmrvSnapshot).where(DmrvSnapshot.project_id == ctx.project_id, DmrvSnapshot.snapshot_hash == snapshot_hash))
    if snapshot is None:
        snapshot = DmrvSnapshot(id=str(uuid.uuid4()), project_id=ctx.project_id, job_id=ctx.job_id,
                                kind=kind, inputs=inputs, outputs=outputs, snapshot_hash=snapshot_hash, created_by=ctx.created_by)
        ctx.db.add(snapshot)
        ctx.db.flush()
    return {"snapshot_id": snapshot.id, "snapshot_hash": snapshot_hash, "context_hash": digest(context)}
