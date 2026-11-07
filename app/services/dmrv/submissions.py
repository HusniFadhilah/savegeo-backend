"""Submission state machine with immutable input locks and reviewer separation."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.dmrv_audit import DmrvIdempotencyKey
from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_job import DmrvSnapshot
from app.db.models.dmrv_submission import DmrvReviewFinding, DmrvSubmission
from app.schemas.dmrv import (
    ReviewFindingCreate,
    ReviewFindingOut,
    ReviewFindingResponse,
    SubmissionCreate,
    SubmissionDecision,
    SubmissionOut,
)
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.evidence import verify_package


def _submission_out(row: DmrvSubmission) -> SubmissionOut:
    return SubmissionOut.model_validate(row, from_attributes=True)


def _finding_out(row: DmrvReviewFinding) -> ReviewFindingOut:
    return ReviewFindingOut.model_validate(row, from_attributes=True)


def _body_hash(payload: SubmissionCreate) -> str:
    raw = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _replay(db: Session, principal: Principal, key: str, body_hash: str) -> SubmissionOut | None:
    record = db.execute(select(DmrvIdempotencyKey).where(
        DmrvIdempotencyKey.principal_key == principal.key,
        DmrvIdempotencyKey.key == key,
    )).scalar_one_or_none()
    if record is None:
        return None
    if record.request_hash != body_hash or record.response_body.get("kind") != "dmrv_submission":
        raise DmrvError(409, "idempotency_key_reused", "Idempotency-Key sudah digunakan untuk permintaan lain.")
    return SubmissionOut.model_validate(record.response_body["body"])


def _finding_replay(db: Session, principal: Principal, key: str, body_hash: str) -> ReviewFindingOut | None:
    record = db.execute(select(DmrvIdempotencyKey).where(
        DmrvIdempotencyKey.principal_key == principal.key,
        DmrvIdempotencyKey.key == key,
    )).scalar_one_or_none()
    if record is None:
        return None
    if record.request_hash != body_hash or record.response_body.get("kind") != "dmrv_review_finding":
        raise DmrvError(409, "idempotency_key_reused", "Idempotency-Key sudah digunakan untuk permintaan lain.")
    return ReviewFindingOut.model_validate(record.response_body["body"])


def create_draft(
    db: Session,
    *,
    project_id: str,
    principal: Principal,
    payload: SubmissionCreate,
    idempotency_key: str,
    request_id: str | None,
) -> tuple[SubmissionOut, bool]:
    authorize(db, project_id, principal, "submission.create")
    request_hash = _body_hash(payload)
    replay = _replay(db, principal, idempotency_key, request_hash)
    if replay is not None:
        return replay, True
    package = db.execute(select(DmrvEvidencePackage).where(
        DmrvEvidencePackage.id == payload.evidence_package_id,
        DmrvEvidencePackage.project_id == project_id,
    )).scalar_one_or_none()
    if package is None:
        raise DmrvError(404, "not_found", "Paket bukti tidak ditemukan.")
    prior = None
    if payload.supersedes_id:
        prior = db.execute(select(DmrvSubmission).where(
            DmrvSubmission.id == payload.supersedes_id,
            DmrvSubmission.project_id == project_id,
        ).with_for_update()).scalar_one_or_none()
        if prior is None:
            raise DmrvError(404, "not_found", "Versi submission sebelumnya tidak ditemukan.")
        if prior.created_by != principal.key or prior.status not in {"changes_requested", "rejected"}:
            raise DmrvError(409, "submission_revision_not_allowed", "Hanya author yang dapat merevisi submission yang diminta perbaikan atau ditolak.")
    version = (db.execute(select(func.max(DmrvSubmission.version)).where(
        DmrvSubmission.project_id == project_id,
    )).scalar_one() or 0) + 1
    snapshot = db.execute(select(DmrvSnapshot).where(
        DmrvSnapshot.id == package.snapshot_id,
        DmrvSnapshot.project_id == project_id,
    )).scalar_one_or_none()
    if snapshot is None:
        raise DmrvError(409, "snapshot_missing", "Snapshot sumber paket bukti tidak tersedia.")
    row = DmrvSubmission(
        id=str(uuid.uuid4()),
        project_id=project_id,
        version=version,
        evidence_package_id=package.id,
        locked_snapshot_hash=snapshot.snapshot_hash,
        locked_manifest_hash=package.manifest_hash,
        locked_inputs={
            "snapshot_id": snapshot.id,
            "snapshot_kind": snapshot.kind,
            "snapshot_hash": snapshot.snapshot_hash,
            "evidence_package_id": package.id,
            "evidence_package_sha256": package.package_sha256,
            "evidence_manifest_sha256": package.manifest_hash,
            "readiness_at_draft": package.readiness,
        },
        status="draft",
        created_by=principal.key,
        supersedes_id=prior.id if prior else None,
        revision=1,
    )
    try:
        db.add(row)
        db.flush()
        out = _submission_out(row)
        record_audit(
            db,
            principal=principal,
            action="submission.draft.create",
            resource_type="dmrv_submission",
            resource_id=row.id,
            project_id=project_id,
            after={"version": row.version, "status": row.status, "locked_inputs": row.locked_inputs},
            snapshot_hash=row.locked_snapshot_hash,
            request_id=request_id,
        )
        db.add(DmrvIdempotencyKey(
            principal_key=principal.key,
            key=idempotency_key,
            request_hash=request_hash,
            response_status=201,
            response_body={"kind": "dmrv_submission", "body": out.model_dump(mode="json")},
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = _replay(db, principal, idempotency_key, request_hash)
        if replay is not None:
            return replay, True
        raise DmrvError(409, "submission_conflict", "Versi submission bentrok dengan perubahan lain.") from None
    except Exception:
        db.rollback()
        raise
    return out, False


def list_submissions(db: Session, *, project_id: str, principal: Principal) -> list[SubmissionOut]:
    authorize(db, project_id, principal, "evidence.read")
    rows = db.execute(select(DmrvSubmission).where(
        DmrvSubmission.project_id == project_id,
    ).order_by(DmrvSubmission.version.desc()).limit(100)).scalars().all()
    return [_submission_out(row) for row in rows]


def list_findings(db: Session, *, project_id: str, submission_id: str, principal: Principal) -> list[ReviewFindingOut]:
    authorize(db, project_id, principal, "evidence.read")
    submission = _submission(db, project_id, submission_id)
    rows = db.execute(select(DmrvReviewFinding).where(
        DmrvReviewFinding.submission_id == submission.id,
    ).order_by(DmrvReviewFinding.created_at, DmrvReviewFinding.id)).scalars().all()
    return [_finding_out(row) for row in rows]


def _submission(db: Session, project_id: str, submission_id: str, *, lock: bool = False) -> DmrvSubmission:
    query = select(DmrvSubmission).where(
        DmrvSubmission.id == submission_id,
        DmrvSubmission.project_id == project_id,
    )
    if lock:
        query = query.with_for_update()
    row = db.execute(query).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Submission tidak ditemukan.")
    return row


def submit(db: Session, *, project_id: str, submission_id: str, principal: Principal, request_id: str | None) -> SubmissionOut:
    authorize(db, project_id, principal, "submission.create")
    row = _submission(db, project_id, submission_id, lock=True)
    if row.created_by != principal.key:
        raise DmrvError(403, "author_required", "Hanya author draft yang dapat mengirim submission.")
    if row.status != "draft":
        if row.submitted_at is not None:
            return _submission_out(row)
        raise DmrvError(409, "submission_state_conflict", "Hanya draft yang dapat dikirim untuk review.")
    package = db.get(DmrvEvidencePackage, row.evidence_package_id)
    if package is None or package.project_id != project_id:
        raise DmrvError(409, "evidence_package_missing", "Paket bukti submission tidak tersedia.")
    if package.readiness != "ready" or package.missing_components:
        raise DmrvError(
            409,
            "evidence_incomplete",
            "Submission belum dapat dikirim karena paket bukti memiliki blocker.",
            details=[{"code": "missing_component", "component": item} for item in package.missing_components],
        )
    if package.manifest_hash != row.locked_manifest_hash:
        raise DmrvError(409, "evidence_snapshot_changed", "Manifest paket bukti berubah setelah draft dibuat.")
    integrity = verify_package(package)
    if not integrity["valid"]:
        raise DmrvError(409, "evidence_integrity_failed", "Paket bukti tidak lolos pemeriksaan checksum.")
    before = row.status
    row.status = "submitted"
    row.submitted_at = dt.datetime.now(dt.UTC)
    row.revision += 1
    record_audit(
        db, principal=principal, action="submission.submit", resource_type="dmrv_submission",
        resource_id=row.id, project_id=project_id, before={"status": before},
        after={"status": row.status, "version": row.version, "manifest_hash": row.locked_manifest_hash},
        snapshot_hash=row.locked_snapshot_hash, request_id=request_id,
    )
    if row.supersedes_id:
        prior = _submission(db, project_id, row.supersedes_id, lock=True)
        previous_status = prior.status
        prior.status = "superseded"
        prior.revision += 1
        record_audit(
            db, principal=principal, action="submission.supersede", resource_type="dmrv_submission",
            resource_id=prior.id, project_id=project_id,
            before={"status": previous_status}, after={"status": prior.status, "superseded_by": row.id},
            snapshot_hash=prior.locked_snapshot_hash, request_id=request_id,
        )
    db.commit()
    db.refresh(row)
    return _submission_out(row)


def start_review(db: Session, *, project_id: str, submission_id: str, principal: Principal, request_id: str | None) -> SubmissionOut:
    authorize(db, project_id, principal, "review.decide")
    row = _submission(db, project_id, submission_id, lock=True)
    if row.created_by == principal.key:
        raise DmrvError(403, "reviewer_must_be_independent", "Author tidak boleh meninjau submission sendiri.")
    if row.status == "under_review" and row.reviewer_key == principal.key:
        return _submission_out(row)
    if row.status not in {"submitted", "changes_requested"}:
        raise DmrvError(409, "submission_state_conflict", "Review hanya dapat dimulai pada submission yang dikirim atau menunggu perbaikan.")
    if row.reviewer_key is not None and row.reviewer_key != principal.key:
        raise DmrvError(409, "review_already_assigned", "Submission sudah ditangani reviewer lain.")
    before_status = row.status
    row.status = "under_review"
    row.reviewer_key = principal.key
    row.revision += 1
    record_audit(
        db, principal=principal, action="submission.review.start", resource_type="dmrv_submission",
        resource_id=row.id, project_id=project_id,
        before={"status": before_status}, after={"status": row.status, "reviewer_key": row.reviewer_key},
        snapshot_hash=row.locked_snapshot_hash, request_id=request_id,
    )
    db.commit()
    db.refresh(row)
    return _submission_out(row)


def _assigned_reviewer(row: DmrvSubmission, principal: Principal) -> None:
    if row.created_by == principal.key:
        raise DmrvError(403, "reviewer_must_be_independent", "Author tidak boleh meninjau submission sendiri.")
    if row.reviewer_key != principal.key:
        raise DmrvError(403, "assigned_reviewer_required", "Aksi review hanya diizinkan reviewer yang memegang review ini.")


def create_finding(
    db: Session, *, project_id: str, submission_id: str, principal: Principal,
    payload: ReviewFindingCreate, idempotency_key: str, request_id: str | None,
) -> tuple[ReviewFindingOut, bool]:
    authorize(db, project_id, principal, "review.decide")
    request_hash = hashlib.sha256(json.dumps({
        "project_id": project_id,
        "submission_id": submission_id,
        **payload.model_dump(mode="json"),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    replay = _finding_replay(db, principal, idempotency_key, request_hash)
    if replay is not None:
        return replay, True
    row = _submission(db, project_id, submission_id, lock=True)
    _assigned_reviewer(row, principal)
    if row.status != "under_review":
        raise DmrvError(409, "submission_state_conflict", "Temuan hanya dapat ditambahkan saat review aktif.")
    finding = DmrvReviewFinding(
        id=str(uuid.uuid4()), submission_id=row.id, code=payload.code.strip(), message=payload.message.strip(),
        status="open", due_date=payload.due_date, evidence_references=[], created_by=principal.key,
    )
    if not finding.code or not finding.message:
        raise DmrvError(422, "invalid_finding", "Kode dan isi temuan wajib diisi.")
    try:
        db.add(finding)
        db.flush()
        out = _finding_out(finding)
        record_audit(
            db, principal=principal, action="review.finding.create", resource_type="dmrv_review_finding",
            resource_id=finding.id, project_id=project_id,
            after={"submission_id": row.id, "code": finding.code, "status": finding.status},
            snapshot_hash=row.locked_snapshot_hash, request_id=request_id,
        )
        db.add(DmrvIdempotencyKey(
            principal_key=principal.key,
            key=idempotency_key,
            request_hash=request_hash,
            response_status=201,
            response_body={"kind": "dmrv_review_finding", "body": out.model_dump(mode="json")},
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = _finding_replay(db, principal, idempotency_key, request_hash)
        if replay is not None:
            return replay, True
        raise DmrvError(409, "review_finding_conflict", "Temuan review berbenturan dengan perubahan lain.") from None
    except Exception:
        db.rollback()
        raise
    db.refresh(finding)
    return _finding_out(finding), False


def respond_to_finding(
    db: Session, *, project_id: str, submission_id: str, finding_id: str,
    principal: Principal, payload: ReviewFindingResponse, request_id: str | None,
) -> ReviewFindingOut:
    authorize(db, project_id, principal, "submission.create")
    row = _submission(db, project_id, submission_id, lock=True)
    if row.created_by != principal.key or row.reviewer_key == principal.key:
        raise DmrvError(403, "submission_author_required", "Temuan hanya dapat dijawab oleh author submission.")
    finding = db.execute(select(DmrvReviewFinding).where(
        DmrvReviewFinding.id == finding_id,
        DmrvReviewFinding.submission_id == row.id,
    ).with_for_update()).scalar_one_or_none()
    if finding is None:
        raise DmrvError(404, "not_found", "Temuan review tidak ditemukan.")
    normalized_response = payload.response.strip()
    if finding.status in {"responded", "resolved"}:
        if finding.response == normalized_response and finding.evidence_references == payload.evidence_references:
            return _finding_out(finding)
        raise DmrvError(409, "finding_state_conflict", "Temuan ini sudah memiliki jawaban.")
    if finding.status != "open" or row.status not in {"under_review", "changes_requested"}:
        raise DmrvError(409, "finding_state_conflict", "Temuan ini tidak menunggu jawaban author.")
    if payload.evidence_references:
        allowed = set(db.execute(select(DmrvEvidencePackage.id).where(
            DmrvEvidencePackage.project_id == project_id,
            DmrvEvidencePackage.id.in_(payload.evidence_references),
        )).scalars().all())
        if allowed != set(payload.evidence_references):
            raise DmrvError(422, "invalid_evidence_reference", "Rujukan bukti harus berupa paket bukti dari proyek ini.")
    before = {"status": finding.status, "response": finding.response}
    finding.response = normalized_response
    finding.evidence_references = payload.evidence_references
    finding.responded_by = principal.key
    finding.status = "responded"
    finding.updated_at = dt.datetime.now(dt.UTC)
    record_audit(
        db, principal=principal, action="review.finding.respond", resource_type="dmrv_review_finding",
        resource_id=finding.id, project_id=project_id, before=before,
        after={"status": finding.status, "evidence_references": finding.evidence_references},
        snapshot_hash=row.locked_snapshot_hash, request_id=request_id,
    )
    db.commit()
    db.refresh(finding)
    return _finding_out(finding)


def resolve_finding(
    db: Session, *, project_id: str, submission_id: str, finding_id: str,
    principal: Principal, request_id: str | None,
) -> ReviewFindingOut:
    authorize(db, project_id, principal, "review.decide")
    row = _submission(db, project_id, submission_id, lock=True)
    _assigned_reviewer(row, principal)
    if row.status != "under_review":
        raise DmrvError(409, "submission_state_conflict", "Temuan hanya dapat diputuskan saat review aktif.")
    finding = db.execute(select(DmrvReviewFinding).where(
        DmrvReviewFinding.id == finding_id,
        DmrvReviewFinding.submission_id == row.id,
    ).with_for_update()).scalar_one_or_none()
    if finding is None:
        raise DmrvError(404, "not_found", "Temuan review tidak ditemukan.")
    if finding.status == "resolved" and finding.resolved_by == principal.key:
        return _finding_out(finding)
    if finding.status != "responded":
        raise DmrvError(409, "finding_state_conflict", "Reviewer hanya dapat menutup temuan setelah author mengirim jawaban.")
    finding.status = "resolved"
    finding.resolved_by = principal.key
    finding.updated_at = dt.datetime.now(dt.UTC)
    record_audit(
        db, principal=principal, action="review.finding.resolve", resource_type="dmrv_review_finding",
        resource_id=finding.id, project_id=project_id,
        before={"status": "responded"}, after={"status": finding.status},
        snapshot_hash=row.locked_snapshot_hash, request_id=request_id,
    )
    db.commit()
    db.refresh(finding)
    return _finding_out(finding)


def decide(
    db: Session, *, project_id: str, submission_id: str, principal: Principal,
    payload: SubmissionDecision, request_id: str | None,
) -> SubmissionOut:
    authorize(db, project_id, principal, "review.decide")
    row = _submission(db, project_id, submission_id, lock=True)
    _assigned_reviewer(row, principal)
    if row.status == payload.decision and row.decision_reason == payload.reason.strip():
        return _submission_out(row)
    if row.status != "under_review":
        raise DmrvError(409, "submission_state_conflict", "Keputusan hanya dapat dibuat pada review yang aktif.")
    findings = db.execute(select(DmrvReviewFinding).where(
        DmrvReviewFinding.submission_id == row.id,
    )).scalars().all()
    pending = [item for item in findings if item.status != "resolved"]
    if payload.decision == "approved" and pending:
        raise DmrvError(409, "review_findings_open", "Semua temuan harus ditutup sebelum approval.", details=[{"finding_id": item.id, "status": item.status} for item in pending])
    if payload.decision == "changes_requested" and not pending:
        raise DmrvError(409, "review_finding_required", "Tambahkan atau buka temuan sebelum meminta perubahan.")
    before = row.status
    row.status = payload.decision
    row.decision_reason = payload.reason.strip()
    row.decided_at = dt.datetime.now(dt.UTC)
    row.revision += 1
    record_audit(
        db, principal=principal, action=f"submission.{payload.decision}", resource_type="dmrv_submission",
        resource_id=row.id, project_id=project_id, before={"status": before},
        after={"status": row.status, "reason": row.decision_reason, "version": row.version},
        snapshot_hash=row.locked_snapshot_hash, request_id=request_id,
    )
    db.commit()
    db.refresh(row)
    return _submission_out(row)
