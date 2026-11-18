"""Project analysis application service: tenant -> project -> feature -> context.

Both HTTP and automation callers use this service. The immutable context is
resolved on the server; no AOI, year, owner, or claimed model version is accepted
from the browser. Provider calls run outside this enqueue transaction.
"""
from __future__ import annotations

import calendar
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.viewer_features import ensure_job_feature_access
from app.db.models.dmrv_boundary import BoundaryVersion
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
    context = {"schema": "project_analysis_v1", "organization_id": project.organization_id, "project_id": project_id,
               "period": {"id": period.id, "revision": period.revision, "start": period.start_date.isoformat(), "end": period.end_date.isoformat()},
               "baseline": {"id": baseline.id, "revision": baseline.revision} if baseline else None,
               "boundary": {"id": boundary.id, "version": boundary.version, "checksum": boundary.checksum, "area_ha": boundary.area_ha},
               "parameters": parameters}
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
