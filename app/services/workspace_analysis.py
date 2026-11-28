"""Project analysis application service: tenant -> project -> feature -> context.

Both HTTP and automation callers use this service. The immutable context is
resolved on the server; no AOI, year, owner, or claimed model version is accepted
from the browser. Provider calls run outside this enqueue transaction.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.viewer_features import ensure_job_feature_access
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_advanced import DmrvFieldCapture
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_integrity import DmrvIntegrityDocument, DmrvIntegrityRecord
from app.db.models.dmrv_job import DmrvJob
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_project import DmrvProject
from app.db.models.organization import Organization
from app.db.models.workspace_analysis import WorkspaceAnalysisRun
from app.schemas.workspace_analysis import ProjectAnalysisCreate
from app.services.dmrv import jobs
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services import workspace_usage
from app.services.analysis_preflight import preflight as run_analysis_preflight


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _methodology_snapshot(project: DmrvProject) -> dict[str, Any]:
    method_id = (project.methodology_id or "").strip()
    version = (project.methodology_version or "").strip()
    canonical = method_id.upper().replace(" ", "")
    supported = canonical in {"VM0047", "VERRAVM0047"} and version.lower().removeprefix("v") == "1.1"
    sources: list[dict] = []
    source_checked_on = None
    if supported:
        from app.services.dmrv.methodology import OFFICIAL_SOURCE_CHECKED_ON, OFFICIAL_SOURCE_DOCUMENTS

        sources = [dict(item) for item in OFFICIAL_SOURCE_DOCUMENTS]
        source_checked_on = OFFICIAL_SOURCE_CHECKED_ON
    blockers = [
        "methodology_specific_net_outcome_calculator_not_implemented",
        "baseline_and_additionality_inputs_required",
        "included_and_excluded_pools_require_method_review",
        "project_emissions_and_leakage_inputs_required",
        "uncertainty_and_reversal_risk_inputs_require_method_review",
    ]
    if not method_id or not version:
        blockers.insert(0, "project_methodology_and_version_not_selected")
    elif not supported:
        blockers.insert(0, "methodology_version_has_no_supported_calculation_profile")
    return {
        "methodology_id": method_id or None,
        "methodology_version": version or None,
        "source_status": "official_sources_pinned" if supported else "unsupported_or_unreviewed",
        "source_checked_on": source_checked_on,
        "official_sources": sources,
        "calculation_status": "not_implemented",
        "net_project_outcome": None,
        "blockers": blockers,
    }


def _sampling_plan_snapshot(db: Session, project_id: str, period_id: str) -> dict[str, Any]:
    rows = db.execute(
        select(DmrvIntegrityRecord)
        .where(DmrvIntegrityRecord.project_id == project_id, DmrvIntegrityRecord.category == "sampling_plan")
        .order_by(DmrvIntegrityRecord.updated_at.desc(), DmrvIntegrityRecord.id.desc())
    ).scalars().all()
    row = next((item for item in rows if (item.payload or {}).get("monitoring_period_id") == period_id), None)
    if row is None:
        row = next((item for item in rows if not (item.payload or {}).get("monitoring_period_id")), None)
    if row is None:
        return {"status": "not_available", "record_id": None, "revision": None, "sha256": None, "strata": []}
    source = {"payload": row.payload or {}, "result": row.result or {}, "status": row.status, "revision": row.revision}
    raw_strata = (row.payload or {}).get("strata", [])
    strata = []
    if isinstance(raw_strata, list):
        for item in raw_strata[:100]:
            if not isinstance(item, dict):
                continue
            result_stratum = next(
                (s for s in (row.result or {}).get("strata", []) if isinstance(s, dict) and s.get("name") == item.get("name")),
                {},
            )
            locations = result_stratum.get("plot_locations", []) if isinstance(result_stratum, dict) else []
            strata.append({
                "name": item.get("name"),
                "area_ha": item.get("area_ha"),
                "expected_cv_pct": item.get("expected_cv_pct"),
                "proposed_plot_count": len(locations) if isinstance(locations, list) else 0,
            })
    return {
        "status": row.status,
        "accepted_for_stratification": row.status == "accepted",
        "record_id": row.id,
        "revision": row.revision,
        "sha256": digest(source),
        "monitoring_period_id": (row.payload or {}).get("monitoring_period_id"),
        "strata": strata,
    }


def _field_evidence_snapshot(db: Session, project_id: str, period: DmrvMonitoringPeriod) -> dict[str, Any]:
    """Freeze references and a digest for monitoring evidence without using it as model input."""
    digest_state = hashlib.sha256()
    measurement_count = 0
    measurement_refs: list[dict] = []
    measurement_query = (
        select(SurveyPlot, SurveyVisit, TreeMeasurement, ImportBatch)
        .join(SurveyVisit, SurveyVisit.plot_id == SurveyPlot.id)
        .outerjoin(TreeMeasurement, TreeMeasurement.visit_id == SurveyVisit.id)
        .outerjoin(ImportBatch, ImportBatch.id == TreeMeasurement.batch_id)
        .where(
            SurveyPlot.project_id == project_id,
            SurveyVisit.visit_date >= period.start_date,
            SurveyVisit.visit_date <= period.end_date,
        )
        .order_by(SurveyVisit.visit_date, SurveyPlot.plot_code, SurveyVisit.id, TreeMeasurement.id)
        .execution_options(yield_per=500)
    )
    for plot, visit, measurement, batch in db.execute(measurement_query):
        record = {
            "plot_id": plot.id,
            "plot_code": plot.plot_code,
            "latitude": plot.latitude,
            "longitude": plot.longitude,
            "land_cover_class": None,
            "visit_id": visit.id,
            "visit_date": visit.visit_date.isoformat(),
            "date_precision": visit.date_precision,
            "measurement_id": measurement.id if measurement else None,
            "tree_code": measurement.tree_code if measurement else None,
            "species": measurement.species if measurement else None,
            "dbh_cm": measurement.dbh_cm if measurement else None,
            "height_m": measurement.height_m if measurement else None,
            "biomass_kg": measurement.biomass_kg if measurement else None,
            "tree_status": measurement.status if measurement else None,
            "qa_flags": measurement.qa_flags if measurement else [],
            "measurement_batch_id": measurement.batch_id if measurement else None,
            "measurement_batch_sha256": batch.sha256 if batch else None,
            "visit_batch_id": visit.batch_id,
        }
        digest_state.update(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        digest_state.update(b"\n")
        measurement_count += 1
        if len(measurement_refs) < 1000:
            measurement_refs.append({"visit_id": visit.id, "measurement_id": measurement.id if measurement else None})

    capture_hash = hashlib.sha256()
    capture_count = 0
    capture_refs: list[dict] = []
    start_at = dt.datetime.combine(period.start_date, dt.time.min, tzinfo=dt.UTC)
    end_at = dt.datetime.combine(period.end_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    capture_query = (
        select(DmrvFieldCapture, DmrvIntegrityDocument)
        .outerjoin(DmrvIntegrityDocument, DmrvIntegrityDocument.id == DmrvFieldCapture.photo_document_id)
        .where(
            DmrvFieldCapture.project_id == project_id,
            DmrvFieldCapture.observed_at >= start_at,
            DmrvFieldCapture.observed_at < end_at,
        )
        .order_by(DmrvFieldCapture.observed_at, DmrvFieldCapture.capture_id)
        .execution_options(yield_per=500)
    )
    for capture, document in db.execute(capture_query):
        record = {
            "capture_id": capture.capture_id,
            "plot_id": capture.plot_id,
            "plot_code": capture.plot_code,
            "observed_at": capture.observed_at.isoformat(),
            "latitude": capture.latitude,
            "longitude": capture.longitude,
            "gps_accuracy_m": capture.gps_accuracy_m,
            "observation": capture.observation,
            "qa_flags": capture.qa_flags,
            "photo_document_id": capture.photo_document_id,
            "photo_sha256": document.sha256 if document else None,
        }
        capture_hash.update(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        capture_hash.update(b"\n")
        capture_count += 1
        if len(capture_refs) < 1000:
            capture_refs.append({"capture_id": capture.capture_id, "photo_document_id": capture.photo_document_id,
                                 "photo_sha256": document.sha256 if document else None})

    total = measurement_count + capture_count
    return {
        "status": "available" if total else "not_available",
        "role": "monitoring_period_evidence_linked_not_used_for_model_inference",
        "survey_record_rows": measurement_count,
        "measurement_sha256": digest_state.hexdigest() if measurement_count else None,
        "measurement_refs": measurement_refs,
        "measurement_refs_truncated": measurement_count > len(measurement_refs),
        "field_capture_rows": capture_count,
        "field_capture_sha256": capture_hash.hexdigest() if capture_count else None,
        "field_capture_refs": capture_refs,
        "field_capture_refs_truncated": capture_count > len(capture_refs),
        "plot_roles": {
            "training": "not_linked_to_project_monitoring_evidence",
            "calibration": "separate_calibration_dataset_and_model_version",
            "validation": "no_independent_validation_record_attached",
            "monitoring": "period_dated_project_field_records",
        },
    }


def _calibration_snapshot(db: Session, parameters: dict) -> dict[str, Any] | None:
    requested = parameters.get("calibration_model_id")
    if requested in (None, ""):
        return None
    from app.db.models.carbon_calibration import CarbonCalibrationModel

    try:
        model_id = int(requested)
    except (TypeError, ValueError):
        return {"requested_model_id": str(requested), "status": "invalid_id"}
    model = db.get(CarbonCalibrationModel, model_id)
    if model is None:
        return {"requested_model_id": model_id, "status": "not_found"}
    metadata = model.metadata_json or {}
    approval = metadata.get("approval_manifest")
    return {
        "requested_model_id": model_id,
        "model_id": model.id,
        "model_version": model.version,
        "artifact_sha256": model.artifact_sha256,
        "target_pool": model.target_pool,
        "target_unit": model.target_unit,
        "status_at_submission": model.status,
        "approval_manifest_sha256": digest(approval) if isinstance(approval, dict) else None,
    }


def _period(db: Session, project_id: str, period_id: str) -> DmrvMonitoringPeriod:
    row = db.scalar(select(DmrvMonitoringPeriod).where(
        DmrvMonitoringPeriod.id == period_id, DmrvMonitoringPeriod.project_id == project_id))
    if row is None:
        raise DmrvError(404, "period_not_found", "Periode proyek tidak ditemukan.")
    if row.status == "planned":
        raise DmrvError(409, "period_not_started", "Mulai periode monitoring sebelum menjalankan analisis.")
    return row


def _monthly(period: DmrvMonitoringPeriod) -> None:
    if (period.start_date.year != period.end_date.year or period.start_date.day != 1
            or period.end_date.day != calendar.monthrange(period.end_date.year, period.end_date.month)[1]):
        raise DmrvError(422, "monthly_period_required", "Analisis ini memerlukan periode bulan penuh dalam satu tahun; tanggal tidak diubah otomatis.")


def submit(db: Session, viewer, project_id: str, request: ProjectAnalysisCreate, key: str, request_id: str | None = None) -> tuple[DmrvJob, bool]:
    try:
        return _submit(db, viewer, project_id, request, key, request_id)
    except IntegrityError:
        # Keys are scoped to the principal across workspaces. Different
        # organization locks may race on the same key; reconcile the winner.
        db.rollback()
        principal = Principal.from_account(viewer)
        authorize(db, project_id, principal, "analysis.run")
        existing = db.scalar(select(DmrvJob).where(DmrvJob.created_by == principal.key, DmrvJob.idempotency_key == key))
        if existing is None:
            raise
        if existing.project_id != project_id or existing.request_hash != digest({"project_id": project_id, **request.model_dump(mode="json")}):
            raise DmrvError(409, "idempotency_key_reused", "Kunci sudah digunakan untuk permintaan berbeda.") from None
        return existing, True


def _submit(db: Session, viewer, project_id: str, request: ProjectAnalysisCreate, key: str, request_id: str | None) -> tuple[DmrvJob, bool]:
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "analysis.run")
    ensure_job_feature_access(viewer, request.kind, db, request.parameters.model_dump())
    if not key or len(key) > 64:
        raise DmrvError(400, "idempotency_key_required", "Idempotency-Key wajib diisi (maks. 64 karakter).")
    project = db.get(DmrvProject, project_id)
    if not project.organization_id:
        raise DmrvError(409, "workspace_required", "Buat proyek dalam organisasi untuk analisis dengan pencatatan kuota.")
    # One organization lock serializes quota and duplicate-key submissions.
    db.execute(select(Organization).where(Organization.id == project.organization_id).with_for_update()).scalar_one()
    request_hash = digest({"project_id": project_id, **request.model_dump(mode="json")})
    existing = db.scalar(select(DmrvJob).where(DmrvJob.created_by == principal.key, DmrvJob.idempotency_key == key))
    if existing:
        if existing.project_id != project_id or existing.request_hash != request_hash:
            raise DmrvError(409, "idempotency_key_reused", "Kunci sudah digunakan untuk permintaan berbeda.")
        db.rollback()
        return existing, True
    period = _period(db, project_id, request.monitoring_period_id)
    if request.kind != "landcover":
        _monthly(period)
    boundary = db.scalar(select(BoundaryVersion).where(
        BoundaryVersion.id == period.boundary_version_id, BoundaryVersion.project_id == project_id))
    if boundary is None:
        raise DmrvError(409, "boundary_missing", "Versi batas periode tidak tersedia.")
    parameters = request.parameters.model_dump(exclude_none=True)
    parameters.update({"aoi": {"geojson": boundary.validated_geojson}, "year": period.start_date.year,
                       "start_month": period.start_date.month, "end_month": period.end_date.month,
                       "start_date": period.start_date.isoformat(), "end_date": period.end_date.isoformat(),
                       "clip_to_aoi": True, "_request_owner": principal.key})
    if request.kind in {"landcover", "landcover_transition"}:
        from app.services.landcover_model_service import MODEL_KEYS, read_model_config
        from app.services.gee_model_release_service import pin_active_landcover_release
        dataset_key = str(parameters.get("dataset") or "Dynamic_World")
        if dataset_key in MODEL_KEYS:
            try:
                pin = pin_active_landcover_release(db, dataset_key)
            except Exception as exc:  # noqa: BLE001 - do not queue work against unverifiable releases
                raise DmrvError(503, "model_lifecycle_unavailable", "Lifecycle model GEE tidak dapat diverifikasi.") from exc
            configured = read_model_config().get(dataset_key, {})
            if pin is None and (configured.get("classifier_asset") or configured.get("annual_asset_template")):
                raise DmrvError(409, "model_not_approved", "Model GEE tutupan lahan belum dievaluasi, direview independen, dan diaktifkan.")
            if pin:
                parameters["_server_model_pin"] = pin
    if request.kind == "carbon":
        # Resolve model identity at submission so a queued project run cannot
        # silently switch to a later active model version.
        from app.api.routes.analysis_jobs import _model_pin_for_job

        model_pin = _model_pin_for_job(db, "carbon", parameters)
        if model_pin:
            parameters["_server_model_pin"] = model_pin
    baseline = None
    if request.kind == "landcover_transition":
        if not request.baseline_period_id:
            raise DmrvError(422, "baseline_required", "Pilih periode baseline untuk perubahan lahan.")
        baseline = _period(db, project_id, request.baseline_period_id)
        _monthly(baseline)
        if (baseline.boundary_version_id != boundary.id or baseline.end_date >= period.start_date
                or baseline.start_date.month != period.start_date.month or baseline.end_date.month != period.end_date.month):
            raise DmrvError(422, "incomparable_periods", "Gunakan batas yang sama, baseline lebih awal, dan bulan observasi yang sebanding.")
        parameters.update(from_year=baseline.start_date.year, to_year=period.start_date.year)
    elif request.baseline_period_id:
        raise DmrvError(422, "unexpected_baseline", "Baseline hanya dipakai untuk perubahan lahan.")
    carbon_provenance = None
    if request.kind == "carbon":
        carbon_provenance = {
            "methodology": _methodology_snapshot(project),
            "sampling_plan": _sampling_plan_snapshot(db, project_id, period.id),
            "field_evidence": _field_evidence_snapshot(db, project_id, period),
            "model_pin": parameters.get("_server_model_pin"),
            "calibration": _calibration_snapshot(db, parameters),
        }
    context = {"schema": "project_analysis_v1", "organization_id": project.organization_id, "project_id": project_id,
               "project_revision": project.revision,
               "period": {"id": period.id, "revision": period.revision, "start": period.start_date.isoformat(), "end": period.end_date.isoformat()},
               "baseline": {"id": baseline.id, "revision": baseline.revision} if baseline else None,
               "boundary": {"id": boundary.id, "version": boundary.version, "checksum": boundary.checksum, "area_ha": boundary.area_ha},
               "parameters": parameters,
               **({"carbon_provenance": carbon_provenance} if carbon_provenance is not None else {})}
    preflight = run_analysis_preflight(request.kind, parameters)
    if preflight["status"] == "blocked":
        raise DmrvError(422, preflight["blocking"][0]["code"],
                        "Preflight mutu memblokir analisis karena masukan belum valid.",
                        details=preflight["blocking"])
    context["preflight"] = preflight
    job = jobs.enqueue(db, project_id=project_id, kind="analysis." + request.kind,
                       payload={"context": context}, created_by=principal.key, idempotency_key=key, request_hash=request_hash)
    db.add(WorkspaceAnalysisRun(job_id=job.id, organization_id=project.organization_id, project_id=project_id,
                               period_id=period.id, boundary_id=boundary.id, context=context, context_hash=digest(context)))
    workspace_usage.reserve(db, project.organization_id, job.id, request.kind, boundary.area_ha)
    record_audit(db, principal=principal, action="analysis.enqueue", resource_type="dmrv_job", resource_id=job.id,
                 project_id=project_id, after={"kind": request.kind, "context_hash": digest(context)}, request_id=request_id)
    db.commit()
    return job, False
