"""dMRV API: /api/dmrv/projects. Thin routes; authorization lives in services/dmrv."""
from __future__ import annotations

import json
import datetime as dt
import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import admin_has_permission, get_current_app_viewer
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_submission import DmrvSubmission
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
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv import jobs as dmrv_jobs
from app.services.dmrv.audit import record_audit
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.field_import import CANONICAL_FIELDS, ImportOptions

router = APIRouter(prefix="/dmrv", tags=["dmrv"])

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
    return [_job_out(row) for row in rows]


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
    return DmrvSnapshotOut.model_validate(row, from_attributes=True)


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
    if not isinstance(viewer, AdminUser) or not admin_has_permission(viewer, CREATE_PERMISSION):
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
        height_unit=height_unit, plot_area_m2=area, area_basis=area_basis or None, default_visit_year=year,  # type: ignore[arg-type]
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
