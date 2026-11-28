"""dMRV API: /api/dmrv/projects. Thin routes; authorization lives in services/dmrv."""
from __future__ import annotations

import json
import csv
import io
import datetime as dt
import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import admin_has_permission, get_current_app_viewer
from app.core.viewer_features import FEATURE_CODES, require_feature
from app.api.routes.dmrv_integrity import router as integrity_router
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_submission import DmrvSubmission
from app.db.models.dmrv_project import DmrvProject
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_integrity import DmrvIntegrityRecord, DmrvRiskPolicy
from app.db.models.workspace_analysis import WorkspaceAnalysisRun
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.dmrv import (
    ArrIllustrativePreviewOut,
    BoundaryCreate,
    BoundaryOut,
    DmrvJobOut,
    DmrvSnapshotOut,
    EvidencePackageCreate,
    EvidencePackageOut,
    EvidencePackageVerificationOut,
    SubmissionCreate,
    SubmissionDecision,
    SubmissionOut,
    ReviewFindingCreate,
    ReviewFindingOut,
    ReviewFindingResponse,
    FieldSummaryJobCreate,
    MemberAdd,
    MemberOut,
    MonitoringPeriodCreate,
    MonitoringPeriodOut,
    MonitoringPeriodUpdate,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.services.dmrv import field_data, monitoring, projects
from app.services.dmrv import evidence as dmrv_evidence
from app.services.dmrv import submissions as dmrv_submissions
from app.services.dmrv.accounting import calculate_arr_illustrative_fixture
from app.services.dmrv.authz import Principal, ROLE_ACTIONS, authorize
from app.services.dmrv import jobs as dmrv_jobs
from app.services.dmrv.audit import record_audit
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.field_import import CANONICAL_FIELDS, ImportOptions

router = APIRouter(
    prefix="/dmrv",
    tags=["dmrv"],
    dependencies=[Depends(require_feature(FEATURE_CODES["dmrv_projects"]))],
)

router.include_router(integrity_router)

CREATE_PERMISSION = "dmrv.project.create"


def _principal(viewer: User | AdminUser) -> Principal:
    return Principal.from_account(viewer)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _etag(response: Response, revision: int) -> None:
    response.headers["ETag"] = f'"{revision}"'


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise DmrvError(428, "if_match_required", "Header If-Match (revisi proyek) wajib diisi.")
    try:
        return int(value.strip().removeprefix("W/").strip('"'))
    except ValueError:
        raise DmrvError(400, "bad_if_match", "Header If-Match harus berisi nomor revisi.") from None


@router.get("/examples/arr-accounting", response_model=ArrIllustrativePreviewOut)
def arr_accounting_example(viewer: User | AdminUser = Depends(get_current_app_viewer)):
    """Return a fixed illustrative example, intentionally independent of projects."""
    del viewer
    ledger = calculate_arr_illustrative_fixture()
    return {
        "example_id": ledger.formula_id,
        "title": "ARR fixed-area accounting example (illustrative only)",
        "source_type": ledger.source_type,
        "qualification": ledger.qualification,
        "formula_id": ledger.formula_id,
        "methodology_profile": ledger.methodology_profile,
        "verification_status": ledger.verification_status,
        "issuance_status": ledger.issuance_status,
        "rounding_policy": ledger.rounding_policy,
        "area_ha": ledger.area_ha,
        "normalized_inputs": [
            {"key": key, "value": value} for key, value in ledger.normalized_inputs
        ],
        "lines": [
            {
                "key": line.key,
                "value": line.value,
                "unit": line.unit,
                "status": line.status,
                "equation": line.equation,
            }
            for line in ledger.lines
        ],
        "blockers": list(ledger.blockers),
    }


def _job_out(row: DmrvJob) -> DmrvJobOut:
    return DmrvJobOut(
        id=row.id, project_id=row.project_id, kind=row.kind, status=row.status, progress=row.progress or {},
        result=row.result, error=row.error, attempts=row.attempts, cancel_requested=row.cancel_requested,
        created_at=row.created_at, started_at=row.started_at, finished_at=row.finished_at,
    )


def _analysis_visible(db: Session, viewer: User | AdminUser, kind: str) -> bool:
    if not kind.startswith("analysis."):
        return True
    from app.core.viewer_features import JOB_TYPE_FEATURES, viewer_feature_codes
    return JOB_TYPE_FEATURES.get(kind.removeprefix("analysis.")) in viewer_feature_codes(db, viewer)


@router.post("/projects/{project_id}/jobs", response_model=DmrvJobOut)
def create_job(
    project_id: str,
    payload: FieldSummaryJobCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    authorize(db, project_id, principal, "field.summary.run")
    if not idempotency_key or len(idempotency_key) > 64:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 64 karakter) wajib diisi.")
    job_payload = {"monitoring_period_id": payload.monitoring_period_id} if payload.monitoring_period_id else {}
    request_body = {"kind": payload.kind, "payload": job_payload}
    request_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = db.execute(
        select(DmrvJob).where(DmrvJob.created_by == principal.key, DmrvJob.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash or existing.project_id != project_id:
            raise DmrvError(409, "idempotency_key_reused", "Kunci idempotensi sudah digunakan untuk permintaan lain.")
        response.status_code = 200
        return _job_out(existing)

    try:
        row = dmrv_jobs.enqueue(
            db, project_id=project_id, kind=payload.kind, payload=job_payload, created_by=principal.key,
            idempotency_key=idempotency_key, request_hash=request_hash,
        )
        record_audit(
            db, principal=principal, action="job.create", resource_type="dmrv_job", resource_id=row.id,
            project_id=project_id, after={"kind": row.kind, "status": row.status}, request_id=_request_id(request),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent request may have won the unique idempotency constraint.
        existing = db.execute(
            select(DmrvJob).where(DmrvJob.created_by == principal.key, DmrvJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is None:
            raise
        if existing.request_hash != request_hash or existing.project_id != project_id:
            raise DmrvError(409, "idempotency_key_reused", "Kunci idempotensi sudah digunakan untuk permintaan lain.") from None
        response.status_code = 200
        return _job_out(existing)
    response.status_code = 202
    return _job_out(row)


@router.get("/projects/{project_id}/jobs", response_model=list[DmrvJobOut])
def list_jobs(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "field.read")
    rows = db.execute(
        select(DmrvJob).where(DmrvJob.project_id == project_id).order_by(DmrvJob.created_at.desc()).limit(limit)
    ).scalars().all()
    return [_job_out(row) for row in rows if _analysis_visible(db, viewer, row.kind)]


@router.get("/projects/{project_id}/jobs/{job_id}", response_model=DmrvJobOut)
def get_job(
    project_id: str,
    job_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "field.read")
    row = db.execute(select(DmrvJob).where(DmrvJob.id == job_id, DmrvJob.project_id == project_id)).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Pekerjaan tidak ditemukan.")
    if not _analysis_visible(db, viewer, row.kind):
        raise DmrvError(403, "feature_forbidden", "Akses fitur analisis tidak diizinkan.")
    return _job_out(row)


@router.post("/projects/{project_id}/jobs/{job_id}/cancel", response_model=DmrvJobOut)
def cancel_job(
    project_id: str,
    job_id: str,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    authorize(db, project_id, principal, "field.summary.run")
    row = db.execute(
        select(DmrvJob).where(DmrvJob.id == job_id, DmrvJob.project_id == project_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Pekerjaan tidak ditemukan.")
    before = row.status
    now = dt.datetime.now(dt.UTC)
    if row.status == "queued":
        row.status, row.finished_at, row.cancel_requested = "cancelled", now, True
        if row.kind.startswith("analysis."):
            from app.services.workspace_usage import settle
            settle(db, row.id, False, now)
    elif row.status == "running":
        row.cancel_requested = True
    record_audit(
        db, principal=principal, action="job.cancel", resource_type="dmrv_job", resource_id=row.id,
        project_id=project_id, before={"status": before}, after={"status": row.status, "cancel_requested": row.cancel_requested},
        request_id=_request_id(request),
    )
    db.commit()
    db.refresh(row)
    return _job_out(row)


@router.get("/projects/{project_id}/snapshots/{snapshot_id}", response_model=DmrvSnapshotOut)
def get_snapshot(
    project_id: str,
    snapshot_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "field.read")
    row = db.execute(
        select(DmrvSnapshot).where(DmrvSnapshot.id == snapshot_id, DmrvSnapshot.project_id == project_id)
    ).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Snapshot tidak ditemukan.")
    if row.kind in {"carbon", "landcover", "landcover_transition", "vegetation"} and not _analysis_visible(db, viewer, "analysis." + row.kind):
        raise DmrvError(403, "feature_forbidden", "Akses fitur analisis tidak diizinkan.")
    snapshot_out = DmrvSnapshotOut.model_validate(row, from_attributes=True)
    snapshot_out.mrv_review_status = _snapshot_review_status(db, project_id, row.id)
    return snapshot_out


def _snapshot_review_status(db: Session, project_id: str, snapshot_id: str) -> dict:
    submission = db.scalar(
        select(DmrvSubmission)
        .join(DmrvEvidencePackage, DmrvEvidencePackage.id == DmrvSubmission.evidence_package_id)
        .where(
            DmrvSubmission.project_id == project_id,
            DmrvEvidencePackage.project_id == project_id,
            DmrvEvidencePackage.snapshot_id == snapshot_id,
            DmrvSubmission.status != "superseded",
        )
        .order_by(DmrvSubmission.version.desc(), DmrvSubmission.created_at.desc())
        .limit(1)
    )
    if submission is None:
        return {
            "status": "not_reviewed",
            "submission_id": None,
            "reviewer_key": None,
            "decided_at": None,
            "decision_reason": None,
            "qualification": "No linked SaveGeo review decision is recorded.",
        }
    return {
        "status": submission.status,
        "submission_id": submission.id,
        "reviewer_key": submission.reviewer_key,
        "decided_at": submission.decided_at.isoformat() if submission.decided_at else None,
        "decision_reason": submission.decision_reason,
        "qualification": "SaveGeo internal project review status; not external verification or issuance.",
    }


@router.get("/projects/{project_id}/snapshots/{snapshot_id}/carbon-statistics.csv")
def export_carbon_snapshot_csv(
    project_id: str,
    snapshot_id: str,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    authorize(db, project_id, principal, "field.read")
    snapshot = db.scalar(select(DmrvSnapshot).where(
        DmrvSnapshot.id == snapshot_id,
        DmrvSnapshot.project_id == project_id,
        DmrvSnapshot.kind == "carbon",
    ))
    if snapshot is None:
        raise DmrvError(404, "not_found", "Snapshot karbon tidak ditemukan.")
    if not _analysis_visible(db, viewer, "analysis.carbon"):
        raise DmrvError(403, "feature_forbidden", "Akses fitur analisis karbon tidak diizinkan.")

    context = (snapshot.inputs or {}).get("context", {})
    result = snapshot.outputs or {}
    mrv = result.get("carbon_mrv") if isinstance(result.get("carbon_mrv"), dict) else {}
    estimate = mrv.get("estimate") if isinstance(mrv.get("estimate"), dict) else {}
    calibration = mrv.get("calibration") if isinstance(mrv.get("calibration"), dict) else {}
    validation = mrv.get("validation") if isinstance(mrv.get("validation"), dict) else {}
    segmentation = mrv.get("segmentation") if isinstance(mrv.get("segmentation"), dict) else {}
    review = mrv.get("review") if isinstance(mrv.get("review"), dict) else {}
    current_review = _snapshot_review_status(db, project_id, snapshot.id)
    accounting = mrv.get("accounting") if isinstance(mrv.get("accounting"), dict) else {}
    registry = mrv.get("registry") if isinstance(mrv.get("registry"), dict) else {}
    carbon = result.get("carbon_estimated") if isinstance(result.get("carbon_estimated"), dict) else {}
    stats = carbon.get("statistics") if isinstance(carbon.get("statistics"), dict) else {}
    area = result.get("area_info") if isinstance(result.get("area_info"), dict) else {}
    fields = [
        "project_id", "monitoring_period_id", "result_version", "snapshot_id", "snapshot_sha256",
        "methodology_id", "methodology_version", "pool", "source_dataset", "source_year",
        "model_name", "model_version", "calibration_status", "calibration_model_id",
        "validation_status", "validation_type", "mrv_review_status", "accounting_status",
        "segmentation_status", "registry_status", "mrv_reviewer", "mrv_reviewed_at", "mrv_review_reason",
        "mean", "std_dev", "min", "max", "total_carbon_tons", "unit",
    ]
    mrv_method = accounting.get("methodology_id")
    if mrv_method is None:
        mrv_method = (context.get("carbon_provenance", {}).get("methodology") or {}).get("methodology_id")
    mrv_version = accounting.get("methodology_version")
    if mrv_version is None:
        mrv_version = (context.get("carbon_provenance", {}).get("methodology") or {}).get("methodology_version")
    row = {
        "project_id": project_id,
        "monitoring_period_id": (context.get("period") or {}).get("id"),
        "result_version": snapshot.snapshot_hash,
        "snapshot_id": snapshot.id,
        "snapshot_sha256": snapshot.snapshot_hash,
        "methodology_id": mrv_method,
        "methodology_version": mrv_version,
        "pool": estimate.get("pool"),
        "source_dataset": estimate.get("source_dataset"),
        "source_year": estimate.get("source_year"),
        "model_name": estimate.get("model_name"),
        "model_version": estimate.get("model_version"),
        "calibration_status": calibration.get("status", "not_recorded"),
        "calibration_model_id": calibration.get("model_id"),
        "validation_status": validation.get("status", "not_validated"),
        "validation_type": validation.get("validation_type", "none"),
        "segmentation_status": segmentation.get("status", "not_linked"),
        "mrv_review_status": current_review.get("status", review.get("status", "not_reviewed")),
        "mrv_reviewer": current_review.get("reviewer_key"),
        "mrv_reviewed_at": current_review.get("decided_at"),
        "mrv_review_reason": current_review.get("decision_reason"),
        "accounting_status": accounting.get("status", "not_calculated"),
        "registry_status": registry.get("status", "not_connected"),
        "mean": stats.get("mean"),
        "std_dev": stats.get("std_dev"),
        "min": stats.get("min"),
        "max": stats.get("max"),
        "total_carbon_tons": area.get("total_carbon_tons"),
        "unit": carbon.get("unit"),
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerow({key: _safe_csv_cell(row.get(key)) for key in fields})
    record_audit(
        db,
        principal=principal,
        action="snapshot.csv.export",
        resource_type="dmrv_snapshot",
        resource_id=snapshot.id,
        project_id=project_id,
        after={"format": "carbon_statistics_csv", "snapshot_hash": snapshot.snapshot_hash},
        snapshot_hash=snapshot.snapshot_hash,
        request_id=_request_id(request),
    )
    db.commit()
    return Response(
        content=stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="savegeo-carbon-{snapshot.id}.csv"'},
    )


def _safe_csv_cell(value):
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


def _evidence_out(row: DmrvEvidencePackage) -> EvidencePackageOut:
    return EvidencePackageOut(
        id=row.id,
        project_id=row.project_id,
        snapshot_id=row.snapshot_id,
        manifest=row.manifest,
        manifest_hash=row.manifest_hash,
        package_sha256=row.package_sha256,
        missing_components=row.missing_components,
        readiness=row.readiness,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.post("/projects/{project_id}/evidence-packages", response_model=EvidencePackageOut)
def create_evidence_package(
    project_id: str,
    payload: EvidencePackageCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    authorize(db, project_id, principal, "evidence.write")
    if not idempotency_key or len(idempotency_key) > 128:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 128 karakter) wajib diisi.")
    request_body = {"project_id": project_id, "snapshot_id": payload.snapshot_id}
    request_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        row, replayed = dmrv_evidence.create_package(
            db,
            project_id=project_id,
            snapshot_id=payload.snapshot_id,
            principal_key=principal.key,
            created_by=principal.key,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            principal=principal,
            request_id=_request_id(request),
        )
    except IntegrityError:
        db.rollback()
        # A concurrent identical request may have committed first.
        existing = db.execute(
            select(DmrvEvidencePackage).where(
                DmrvEvidencePackage.principal_key == principal.key,
                DmrvEvidencePackage.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        if existing.request_hash != request_hash or existing.project_id != project_id:
            raise DmrvError(409, "idempotency_key_reused", "Kunci idempotensi sudah digunakan untuk permintaan lain.") from None
        row, replayed = existing, True
    response.status_code = 200 if replayed else 201
    return _evidence_out(row)


@router.get("/projects/{project_id}/evidence-packages", response_model=list[EvidencePackageOut])
def list_evidence_packages(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "evidence.read")
    rows = db.execute(
        select(DmrvEvidencePackage)
        .where(DmrvEvidencePackage.project_id == project_id)
        .order_by(DmrvEvidencePackage.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_evidence_out(row) for row in rows]


def _get_evidence_package(db: Session, project_id: str, package_id: str) -> DmrvEvidencePackage:
    row = db.execute(
        select(DmrvEvidencePackage).where(
            DmrvEvidencePackage.id == package_id,
            DmrvEvidencePackage.project_id == project_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Paket bukti tidak ditemukan.")
    return row


@router.get(
    "/projects/{project_id}/evidence-packages/{package_id}",
    response_model=EvidencePackageOut,
)
def get_evidence_package(
    project_id: str,
    package_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "evidence.read")
    return _evidence_out(_get_evidence_package(db, project_id, package_id))


@router.get(
    "/projects/{project_id}/evidence-packages/{package_id}/verify",
    response_model=EvidencePackageVerificationOut,
)
def verify_evidence_package(
    project_id: str,
    package_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "evidence.read")
    row = _get_evidence_package(db, project_id, package_id)
    return {"package_id": row.id, **dmrv_evidence.verify_package(row)}


@router.get("/projects/{project_id}/evidence-packages/{package_id}/download")
def download_evidence_package(
    project_id: str,
    package_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "evidence.read")
    row = _get_evidence_package(db, project_id, package_id)
    integrity = dmrv_evidence.verify_package(row)
    if not integrity["valid"]:
        raise DmrvError(409, "evidence_integrity_failed", "Checksum paket bukti gagal diverifikasi.")
    path = dmrv_evidence.package_path(row)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"savegeo-dmrv-evidence-{row.id}.zip",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/projects/{project_id}/submissions", response_model=SubmissionOut)
def create_submission_draft(
    project_id: str,
    payload: SubmissionCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 128 karakter) wajib diisi.")
    row, replayed = dmrv_submissions.create_draft(
        db, project_id=project_id, principal=_principal(viewer), payload=payload,
        idempotency_key=idempotency_key, request_id=_request_id(request),
    )
    response.status_code = 200 if replayed else 201
    return row


@router.get("/projects/{project_id}/submissions", response_model=list[SubmissionOut])
def list_submissions(
    project_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.list_submissions(db, project_id=project_id, principal=_principal(viewer))


@router.get("/projects/{project_id}/submissions/{submission_id}", response_model=SubmissionOut)
def get_submission(
    project_id: str,
    submission_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    authorize(db, project_id, _principal(viewer), "evidence.read")
    # The dedicated list/read service filters both ids to the authorized project.
    row = db.execute(select(DmrvSubmission).where(
        DmrvSubmission.id == submission_id,
        DmrvSubmission.project_id == project_id,
    )).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Submission tidak ditemukan.")
    return SubmissionOut.model_validate(row, from_attributes=True)


@router.post("/projects/{project_id}/submissions/{submission_id}/submit", response_model=SubmissionOut)
def submit_submission(
    project_id: str,
    submission_id: str,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.submit(
        db, project_id=project_id, submission_id=submission_id,
        principal=_principal(viewer), request_id=_request_id(request),
    )


@router.post("/projects/{project_id}/submissions/{submission_id}/review/start", response_model=SubmissionOut)
def start_submission_review(
    project_id: str,
    submission_id: str,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.start_review(
        db, project_id=project_id, submission_id=submission_id,
        principal=_principal(viewer), request_id=_request_id(request),
    )


@router.get(
    "/projects/{project_id}/submissions/{submission_id}/findings",
    response_model=list[ReviewFindingOut],
)
def list_submission_findings(
    project_id: str,
    submission_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.list_findings(
        db, project_id=project_id, submission_id=submission_id, principal=_principal(viewer),
    )


@router.post(
    "/projects/{project_id}/submissions/{submission_id}/findings",
    response_model=ReviewFindingOut,
    status_code=201,
)
def create_submission_finding(
    project_id: str,
    submission_id: str,
    payload: ReviewFindingCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 128 karakter) wajib diisi.")
    row, replayed = dmrv_submissions.create_finding(
        db, project_id=project_id, submission_id=submission_id,
        principal=_principal(viewer), payload=payload, idempotency_key=idempotency_key,
        request_id=_request_id(request),
    )
    response.status_code = 200 if replayed else 201
    return row


@router.post(
    "/projects/{project_id}/submissions/{submission_id}/findings/{finding_id}/respond",
    response_model=ReviewFindingOut,
)
def respond_submission_finding(
    project_id: str,
    submission_id: str,
    finding_id: str,
    payload: ReviewFindingResponse,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.respond_to_finding(
        db, project_id=project_id, submission_id=submission_id, finding_id=finding_id,
        principal=_principal(viewer), payload=payload, request_id=_request_id(request),
    )


@router.post(
    "/projects/{project_id}/submissions/{submission_id}/findings/{finding_id}/resolve",
    response_model=ReviewFindingOut,
)
def resolve_submission_finding(
    project_id: str,
    submission_id: str,
    finding_id: str,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.resolve_finding(
        db, project_id=project_id, submission_id=submission_id, finding_id=finding_id,
        principal=_principal(viewer), request_id=_request_id(request),
    )


@router.post("/projects/{project_id}/submissions/{submission_id}/decision", response_model=SubmissionOut)
def decide_submission_review(
    project_id: str,
    submission_id: str,
    payload: SubmissionDecision,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return dmrv_submissions.decide(
        db, project_id=project_id, submission_id=submission_id,
        principal=_principal(viewer), payload=payload, request_id=_request_id(request),
    )


@router.post("/projects/{project_id}/monitoring-periods", response_model=MonitoringPeriodOut)
def create_monitoring_period(
    project_id: str,
    payload: MonitoringPeriodCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 128 karakter) wajib diisi.")
    out, replayed = monitoring.create_period(
        db, _principal(viewer), project_id, payload, idempotency_key, _request_id(request),
    )
    response.status_code = 200 if replayed else 201
    _etag(response, out.revision)
    return out


@router.get("/projects/{project_id}/monitoring-periods", response_model=list[MonitoringPeriodOut])
def list_monitoring_periods(
    project_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return monitoring.list_periods(db, _principal(viewer), project_id)


@router.get("/projects/{project_id}/monitoring-periods/{period_id}", response_model=MonitoringPeriodOut)
def get_monitoring_period(
    project_id: str,
    period_id: str,
    response: Response,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    out = monitoring.get_period(db, _principal(viewer), project_id, period_id)
    _etag(response, out.revision)
    return out


@router.patch("/projects/{project_id}/monitoring-periods/{period_id}", response_model=MonitoringPeriodOut)
def update_monitoring_period(
    project_id: str,
    period_id: str,
    payload: MonitoringPeriodUpdate,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    out = monitoring.update_period(
        db, _principal(viewer), project_id, period_id, payload, _parse_if_match(if_match), _request_id(request),
    )
    _etag(response, out.revision)
    return out


@router.post("/projects", response_model=ProjectOut)
def create_project(
    payload: ProjectCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    if payload.organization_id:
        from app.services.organizations import require_member
        require_member(db, payload.organization_id, Principal.from_account(viewer), manage=True)
    elif not isinstance(viewer, AdminUser) or not admin_has_permission(viewer, CREATE_PERMISSION):
        raise DmrvError(403, "forbidden", "Akun ini tidak diizinkan membuat proyek dMRV.")
    if not idempotency_key or len(idempotency_key) > 128:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 128 karakter) wajib diisi.")
    out, replayed = projects.create_project(db, _principal(viewer), payload, idempotency_key, _request_id(request))
    response.status_code = 200 if replayed else 201
    _etag(response, out.revision)
    return out


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return projects.list_projects(db, _principal(viewer))


@router.get("/projects/{project_id}/dashboard")
def project_dashboard(
    project_id: str,
    period_from: dt.date | None = Query(default=None),
    period_to: dt.date | None = Query(default=None),
    recent_limit: int = Query(default=20, ge=1, le=100),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    """One membership-scoped summary for project, periods, jobs, reports and risks."""
    principal = _principal(viewer)
    project_role = authorize(db, project_id, principal, "project.read")
    role_actions = ROLE_ACTIONS.get(project_role, frozenset())
    can_read_evidence = "evidence.read" in role_actions
    can_read_accounting = "accounting.read" in role_actions
    if period_from and period_to and period_to < period_from:
        raise DmrvError(422, "invalid_period_filter", "period_to harus sama atau setelah period_from.")
    project = db.get(DmrvProject, project_id)
    if project is None:
        raise DmrvError(404, "not_found", "Proyek tidak ditemukan.")

    def datetime_filters(column):
        filters = []
        if period_from:
            filters.append(column >= dt.datetime.combine(period_from, dt.time.min, tzinfo=dt.UTC))
        if period_to:
            filters.append(column < dt.datetime.combine(period_to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC))
        return filters

    period_query = select(DmrvMonitoringPeriod).where(DmrvMonitoringPeriod.project_id == project_id)
    if period_from:
        period_query = period_query.where(DmrvMonitoringPeriod.end_date >= period_from)
    if period_to:
        period_query = period_query.where(DmrvMonitoringPeriod.start_date <= period_to)
    matching_periods = db.execute(period_query.order_by(DmrvMonitoringPeriod.start_date.desc())).scalars().all()
    from app.core.viewer_features import JOB_TYPE_FEATURES, viewer_feature_codes
    allowed_analysis_kinds = ["analysis." + kind for kind, code in JOB_TYPE_FEATURES.items()
                              if code in viewer_feature_codes(db, viewer)]
    job_filters = [DmrvJob.project_id == project_id, *datetime_filters(DmrvJob.created_at),
                   or_(~DmrvJob.kind.like("analysis.%"), DmrvJob.kind.in_(allowed_analysis_kinds))]
    job_status_rows = db.execute(select(DmrvJob.status, func.count()).where(*job_filters)
                                 .group_by(DmrvJob.status)).all()
    job_type_rows = db.execute(select(DmrvJob.kind, func.count()).where(*job_filters)
                               .group_by(DmrvJob.kind)).all()
    jobs_visible = db.execute(select(DmrvJob).where(*job_filters)
        .order_by(DmrvJob.created_at.desc()).limit(recent_limit)).scalars().all()
    evidence_total = evidence_ready = 0
    submissions_total = risk_total = snapshot_total = 0
    submission_status_counts: dict[str, int] = {}
    evidence, submissions, risks = [], [], []
    if can_read_evidence:
        evidence_filters = [DmrvEvidencePackage.project_id == project_id, *datetime_filters(DmrvEvidencePackage.created_at)]
        evidence_total = db.execute(select(func.count()).select_from(DmrvEvidencePackage).where(*evidence_filters)).scalar_one()
        evidence_ready = db.execute(select(func.count()).select_from(DmrvEvidencePackage)
            .where(*evidence_filters, DmrvEvidencePackage.readiness == "ready")).scalar_one()
        evidence = db.execute(select(DmrvEvidencePackage).where(*evidence_filters)
            .order_by(DmrvEvidencePackage.created_at.desc()).limit(recent_limit)).scalars().all()
        submission_filters = [DmrvSubmission.project_id == project_id, *datetime_filters(DmrvSubmission.created_at)]
        submission_rows = db.execute(select(DmrvSubmission.status, func.count()).where(*submission_filters)
                                     .group_by(DmrvSubmission.status)).all()
        submission_status_counts = {status: int(count) for status, count in submission_rows}
        submissions_total = sum(submission_status_counts.values())
        submissions = db.execute(select(DmrvSubmission).where(*submission_filters)
            .order_by(DmrvSubmission.created_at.desc()).limit(recent_limit)).scalars().all()
    if can_read_accounting:
        risk_filters = [DmrvIntegrityRecord.project_id == project_id, DmrvIntegrityRecord.category == "risk",
                        *datetime_filters(DmrvIntegrityRecord.updated_at)]
        risk_total = db.execute(select(func.count()).select_from(DmrvIntegrityRecord).where(*risk_filters)).scalar_one()
        risks = db.execute(select(DmrvIntegrityRecord).where(*risk_filters)
            .order_by(DmrvIntegrityRecord.updated_at.desc()).limit(recent_limit)).scalars().all()
    risk_policy = db.get(DmrvRiskPolicy, project_id) if can_read_accounting else None
    snapshot_filters = [DmrvSnapshot.project_id == project_id, *datetime_filters(DmrvSnapshot.created_at)]
    snapshot_total = db.execute(select(func.count()).select_from(DmrvSnapshot).where(*snapshot_filters)).scalar_one()
    snapshots = db.execute(select(DmrvSnapshot).where(*snapshot_filters)
        .order_by(DmrvSnapshot.created_at.desc()).limit(recent_limit)).scalars().all()
    visible_period_ids = {row.id for row in matching_periods}
    jobs_by_period: dict[str, dict[str, int]] = {}
    if visible_period_ids:
        period_job_rows = db.execute(select(WorkspaceAnalysisRun.period_id, DmrvJob.status, func.count())
            .join(DmrvJob, DmrvJob.id == WorkspaceAnalysisRun.job_id)
            .where(*job_filters, WorkspaceAnalysisRun.period_id.in_(visible_period_ids))
            .group_by(WorkspaceAnalysisRun.period_id, DmrvJob.status)).all()
        for period_id, status, count in period_job_rows:
            jobs_by_period.setdefault(period_id, {})[status] = int(count)
    job_counts = {status: int(count) for status, count in job_status_rows}
    job_types = {kind: int(count) for kind, count in job_type_rows}
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "filter": {"period_from": period_from.isoformat() if period_from else None,
                   "period_to": period_to.isoformat() if period_to else None},
        "project": {"id": project.id, "name": project.name, "organization_id": project.organization_id,
                    "activity_type": project.activity_type, "jurisdiction": project.jurisdiction,
                    "status": project.status, "methodology_id": project.methodology_id,
                    "revision": project.revision, "updated_at": project.updated_at.isoformat() if project.updated_at else None},
        "summary": {"period_count": len(matching_periods), "job_count": sum(job_counts.values()),
                    "report_count": int(evidence_total), "submission_count": int(submissions_total),
                    "risk_count": int(risk_total), "snapshot_count": int(snapshot_total),
                    "jobs_by_status": job_counts, "jobs_by_type": job_types,
                    "reports_ready": int(evidence_ready),
                    "submissions_by_status": submission_status_counts},
        "periods": [{"id": row.id, "name": row.name, "start_date": row.start_date.isoformat(),
                     "end_date": row.end_date.isoformat(), "status": row.status, "revision": row.revision,
                     "job_counts": jobs_by_period.get(row.id, {})} for row in matching_periods],
        "recent_jobs": [{"id": row.id, "kind": row.kind, "status": row.status,
                         "created_at": row.created_at.isoformat(), "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                         "progress": row.progress or {}, "error": row.error}
                        for row in jobs_visible],
        "reports": {"evidence_packages": [{"id": row.id, "snapshot_id": row.snapshot_id,
                         "readiness": row.readiness, "missing_components": row.missing_components,
                         "created_at": row.created_at.isoformat()} for row in evidence[:recent_limit]],
                    "submissions": [{"id": row.id, "version": row.version, "status": row.status,
                                     "created_at": row.created_at.isoformat(), "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
                                     "decided_at": row.decided_at.isoformat() if row.decided_at else None}
                                    for row in submissions[:recent_limit]]},
        "risks": {"policy": {"enabled": risk_policy.enabled, "interval_days": risk_policy.interval_days,
                              "lookback_days": risk_policy.lookback_days, "next_run_at": risk_policy.next_run_at.isoformat() if risk_policy.next_run_at else None}
                              if risk_policy else None,
                  "records": [{"id": row.id, "key": row.record_key, "title": row.title, "status": row.status,
                               "revision": row.revision, "result": row.result, "updated_at": row.updated_at.isoformat()}
                              for row in risks]},
        "latest_snapshots": [{"id": row.id, "kind": row.kind, "hash": row.snapshot_hash,
                              "created_at": row.created_at.isoformat()} for row in snapshots],
    }


@router.get("/dashboard")
def portfolio_dashboard(
    organization_id: str | None = Query(default=None, min_length=1, max_length=36),
    period_from: dt.date | None = Query(default=None),
    period_to: dt.date | None = Query(default=None),
    recent_limit: int = Query(default=30, ge=1, le=100),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    """Portfolio dashboard across only projects visible to this principal."""
    if period_from and period_to and period_to < period_from:
        raise DmrvError(422, "invalid_period_filter", "period_to harus sama atau setelah period_from.")
    visible_projects = projects.list_projects(db, _principal(viewer))
    if organization_id:
        from app.services.organizations import require_member
        require_member(db, organization_id, _principal(viewer))
        visible_projects = [p for p in visible_projects if p.organization_id == organization_id]
    ids = [p.id for p in visible_projects]
    if not ids:
        return {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "filter": {"organization_id": organization_id,
                "period_from": period_from.isoformat() if period_from else None,
                "period_to": period_to.isoformat() if period_to else None},
                "summary": {"project_count": 0, "job_count": 0, "report_count": 0, "submission_count": 0,
                            "risk_count": 0, "period_count": 0, "reports_ready": 0, "jobs_by_status": {}},
                "projects": [], "recent_jobs": [], "recent_periods": [], "recent_reports": [],
                "recent_submissions": [], "recent_risks": []}
    job_filters = [DmrvJob.project_id.in_(ids)]
    if period_from:
        job_filters.append(DmrvJob.created_at >= dt.datetime.combine(period_from, dt.time.min, tzinfo=dt.UTC))
    if period_to:
        job_filters.append(DmrvJob.created_at < dt.datetime.combine(period_to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC))
    from app.core.viewer_features import JOB_TYPE_FEATURES, viewer_feature_codes
    allowed_analysis_kinds = ["analysis." + kind for kind, code in JOB_TYPE_FEATURES.items()
                              if code in viewer_feature_codes(db, viewer)]
    job_filters.append(or_(~DmrvJob.kind.like("analysis.%"), DmrvJob.kind.in_(allowed_analysis_kinds)))
    job_counts_rows = db.execute(select(DmrvJob.project_id, DmrvJob.status, func.count())
                                 .where(*job_filters).group_by(DmrvJob.project_id, DmrvJob.status)).all()
    visible_jobs = db.execute(select(DmrvJob).where(*job_filters).order_by(DmrvJob.created_at.desc()).limit(2000)).scalars().all()
    visible_jobs = [j for j in visible_jobs if _analysis_visible(db, viewer, j.kind)]
    status_by_project: dict[str, dict[str, int]] = {}
    for pid, status, count in job_counts_rows:
        status_by_project.setdefault(pid, {})[status] = int(count)
    period_rows = db.execute(select(DmrvMonitoringPeriod).where(DmrvMonitoringPeriod.project_id.in_(ids))
        .order_by(DmrvMonitoringPeriod.start_date.desc())).scalars().all()
    if period_from:
        period_rows = [p for p in period_rows if p.end_date >= period_from]
    if period_to:
        period_rows = [p for p in period_rows if p.start_date <= period_to]
    project_roles = {project.id: project.my_role for project in visible_projects}
    evidence_project_ids = [pid for pid, role in project_roles.items()
                            if "evidence.read" in ROLE_ACTIONS.get(role or "", frozenset())]
    accounting_project_ids = [pid for pid, role in project_roles.items()
                              if "accounting.read" in ROLE_ACTIONS.get(role or "", frozenset())]
    def date_filters(column):
        filters = []
        if period_from:
            filters.append(column >= dt.datetime.combine(period_from, dt.time.min, tzinfo=dt.UTC))
        if period_to:
            filters.append(column < dt.datetime.combine(period_to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC))
        return filters

    evidence_counts = {}
    submission_counts = {}
    report_ready_count = 0
    evidence_rows = []
    submission_rows = []
    if evidence_project_ids:
        evidence_base = [DmrvEvidencePackage.project_id.in_(evidence_project_ids),
                         *date_filters(DmrvEvidencePackage.created_at)]
        evidence_count_rows = db.execute(select(DmrvEvidencePackage.project_id, func.count())
            .where(*evidence_base).group_by(DmrvEvidencePackage.project_id)).all()
        evidence_counts = {pid: int(count) for pid, count in evidence_count_rows}
        report_ready_count = db.execute(select(func.count()).select_from(DmrvEvidencePackage)
            .where(*evidence_base, DmrvEvidencePackage.readiness == "ready")).scalar_one()
        evidence_rows = db.execute(select(DmrvEvidencePackage).where(*evidence_base)
            .order_by(DmrvEvidencePackage.created_at.desc()).limit(recent_limit)).scalars().all()
        submission_base = [DmrvSubmission.project_id.in_(evidence_project_ids),
                           *date_filters(DmrvSubmission.created_at)]
        submission_count_rows = db.execute(select(DmrvSubmission.project_id, func.count())
            .where(*submission_base).group_by(DmrvSubmission.project_id)).all()
        submission_counts = {pid: int(count) for pid, count in submission_count_rows}
        submission_rows = db.execute(select(DmrvSubmission).where(*submission_base)
            .order_by(DmrvSubmission.created_at.desc()).limit(recent_limit)).scalars().all()

    risk_counts = {}
    risk_rows = []
    if accounting_project_ids:
        risk_base = [DmrvIntegrityRecord.project_id.in_(accounting_project_ids),
                     DmrvIntegrityRecord.category == "risk", *date_filters(DmrvIntegrityRecord.updated_at)]
        risk_count_rows = db.execute(select(DmrvIntegrityRecord.project_id, func.count())
            .where(*risk_base).group_by(DmrvIntegrityRecord.project_id)).all()
        risk_counts = {pid: int(count) for pid, count in risk_count_rows}
        risk_rows = db.execute(select(DmrvIntegrityRecord).where(*risk_base)
            .order_by(DmrvIntegrityRecord.updated_at.desc()).limit(recent_limit)).scalars().all()
    projects_out = []
    for project in visible_projects:
        per_project_jobs = status_by_project.get(project.id, {})
        projects_out.append({"id": project.id, "name": project.name, "organization_id": project.organization_id,
            "status": project.status, "activity_type": project.activity_type,
            "monitoring_period_count": sum(p.project_id == project.id for p in period_rows),
            "job_counts": per_project_jobs, "job_count": sum(per_project_jobs.values()),
            "report_count": evidence_counts.get(project.id, 0),
            "submission_count": submission_counts.get(project.id, 0),
            "risk_count": risk_counts.get(project.id, 0)})
    global_status: dict[str, int] = {}
    for counts in status_by_project.values():
        for status, count in counts.items():
            global_status[status] = global_status.get(status, 0) + count
    return {"generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "filter": {"organization_id": organization_id, "period_from": period_from.isoformat() if period_from else None,
                   "period_to": period_to.isoformat() if period_to else None},
        "summary": {"project_count": len(visible_projects), "job_count": sum(global_status.values()),
                    "report_count": sum(evidence_counts.values()), "submission_count": sum(submission_counts.values()),
                    "risk_count": sum(risk_counts.values()), "period_count": len(period_rows), "jobs_by_status": global_status,
                    "reports_ready": int(report_ready_count)},
        "projects": projects_out,
        "recent_jobs": [{"id": row.id, "project_id": row.project_id, "kind": row.kind, "status": row.status,
                         "created_at": row.created_at.isoformat(), "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                         "progress": row.progress or {}, "error": row.error}
                        for row in visible_jobs[:recent_limit]],
        "recent_periods": [{"id": row.id, "project_id": row.project_id, "name": row.name,
                             "start_date": row.start_date.isoformat(), "end_date": row.end_date.isoformat(),
                             "status": row.status}
                            for row in period_rows[:recent_limit]],
        "recent_reports": [{"id": row.id, "project_id": row.project_id, "snapshot_id": row.snapshot_id,
                             "readiness": row.readiness, "created_at": row.created_at.isoformat()}
                            for row in evidence_rows],
        "recent_submissions": [{"id": row.id, "project_id": row.project_id, "version": row.version,
                                 "status": row.status, "created_at": row.created_at.isoformat()}
                                for row in submission_rows],
        "recent_risks": [{"id": row.id, "project_id": row.project_id, "title": row.title,
                           "status": row.status, "result": row.result, "updated_at": row.updated_at.isoformat()}
                          for row in sorted(risk_rows, key=lambda item: item.updated_at, reverse=True)[:recent_limit]]}


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    response: Response,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    out = projects.get_project(db, _principal(viewer), project_id)
    _etag(response, out.revision)
    return out


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    # Existence/membership is checked before header validation so a non-member
    # learns nothing from the difference between 404 and 428.
    projects.get_project(db, principal, project_id)
    out = projects.update_project(db, principal, project_id, payload, _parse_if_match(if_match), _request_id(request))
    _etag(response, out.revision)
    return out


@router.post("/projects/{project_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    project_id: str,
    payload: MemberAdd,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return projects.add_member(db, _principal(viewer), project_id, payload, _request_id(request))


@router.post("/projects/{project_id}/boundaries", response_model=BoundaryOut, status_code=201)
def add_boundary(
    project_id: str,
    payload: BoundaryCreate,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return projects.add_boundary(db, _principal(viewer), project_id, payload.geojson, payload.reason, _request_id(request))


@router.get("/projects/{project_id}/boundaries", response_model=list[BoundaryOut])
def list_boundaries(
    project_id: str,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return projects.list_boundaries(db, _principal(viewer), project_id)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _read_upload(file: UploadFile) -> bytes:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise DmrvError(413, "file_too_large", "Ukuran berkas maksimal 10 MB.")
    return data


def _parse_mapping(text: str | None) -> dict[str, str] | None:
    if not text:
        return None
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError:
        raise DmrvError(422, "bad_mapping", "Mapping kolom bukan JSON yang valid.") from None
    if (
        not isinstance(mapping, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
        or set(mapping) - set(CANONICAL_FIELDS)
    ):
        raise DmrvError(
            422, "bad_mapping",
            "Mapping harus objek {field: nama_kolom}; field dikenal: " + ", ".join(CANONICAL_FIELDS) + ".",
        )
    return mapping


def _options(height_unit: str, plot_area_m2: str | None, area_basis: str | None, default_visit_year: str | None) -> ImportOptions:
    try:
        area = float(plot_area_m2) if plot_area_m2 else None
        year = int(default_visit_year) if default_visit_year else None
    except ValueError:
        raise DmrvError(422, "bad_options", "plot_area_m2 dan default_visit_year harus berupa angka.") from None
    if height_unit not in ("m", "cm") or area_basis not in (None, "", "measured", "assumed") or (area is not None and area <= 0):
        raise DmrvError(422, "bad_options", "height_unit: m|cm; area_basis: measured|assumed; plot_area_m2 > 0.")
    return ImportOptions(
        height_unit=height_unit, plot_area_m2=area, area_basis=area_basis or None, default_visit_year=year,
    )


@router.post("/projects/{project_id}/field-data/imports/preview")
def preview_field_import(
    project_id: str,
    file: UploadFile = File(...),
    mapping: str | None = Form(default=None),
    height_unit: str = Form(default="m"),
    plot_area_m2: str | None = Form(default=None),
    area_basis: str | None = Form(default=None),
    default_visit_year: str | None = Form(default=None),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    authorize(db, project_id, principal, "field.write")
    return field_data.preview_import(
        db, principal, project_id, file.filename or "upload", _read_upload(file), _parse_mapping(mapping),
        _options(height_unit, plot_area_m2, area_basis, default_visit_year),
    )


@router.post("/projects/{project_id}/field-data/imports")
def commit_field_import(
    project_id: str,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    mapping: str | None = Form(default=None),
    mode: Literal["atomic", "partial"] = Form(default="atomic"),
    height_unit: str = Form(default="m"),
    plot_area_m2: str | None = Form(default=None),
    area_basis: str | None = Form(default=None),
    default_visit_year: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = _principal(viewer)
    authorize(db, project_id, principal, "field.write")
    if not idempotency_key or len(idempotency_key) > 64:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 64 karakter) wajib diisi.")
    result = field_data.commit_import(
        db, principal, project_id, file.filename or "upload", _read_upload(file), _parse_mapping(mapping),
        _options(height_unit, plot_area_m2, area_basis, default_visit_year), mode, _request_id(request), idempotency_key,
    )
    response.status_code = 200 if result.pop("replayed", False) else 201
    return result


@router.get("/projects/{project_id}/field-data/plots")
def list_field_plots(
    project_id: str, viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    return field_data.list_plots(db, _principal(viewer), project_id)


@router.get("/projects/{project_id}/field-data/plots/{plot_code}/visits")
def list_field_visits(
    project_id: str, plot_code: str, viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return field_data.list_visits(db, _principal(viewer), project_id, plot_code)
