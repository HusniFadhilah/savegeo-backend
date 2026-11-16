"""The seven integrity workflows, under /api/dmrv; no admin bypass."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.core.security import get_current_app_viewer
from app.db.models.dmrv_integrity import DmrvIntegrityDocument, DmrvIntegrityRevision
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.user import User
from app.db.models.admin_user import AdminUser
from app.db.session import get_db
from app.schemas.dmrv_integrity import RecordOut, RecordReview, RecordSave, RiskPolicyInput, RiskScanInput
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv import integrity
from app.services.dmrv.integrity_risks import enqueue_scan, save_policy

router = APIRouter()


@router.get("/projects/{project_id}/integrity/members")
def list_members(project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    authorize(db, project_id, Principal.from_account(viewer), "members.manage")
    rows = db.execute(select(ProjectMember).where(ProjectMember.project_id == project_id)).scalars().all()
    output = []
    for row in rows:
        account = db.get(User if row.principal_type == "user" else AdminUser, row.principal_id)
        output.append(
            {
                "principal_type": row.principal_type,
                "principal_id": row.principal_id,
                "username": account.username if account else "",
                "role": row.role,
            }
        )
    return output


@router.delete("/projects/{project_id}/integrity/members/{principal_type}/{principal_id}")
def revoke_member(
    project_id: str,
    principal_type: str,
    principal_id: int,
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "members.manage")
    if (principal.type, principal.id) == (principal_type, principal_id):
        raise DmrvError(422, "self_revoke", "Pemilik tidak dapat mencabut aksesnya sendiri.")
    row = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_type == principal_type,
            ProjectMember.principal_id == principal_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Anggota tidak ditemukan.")
    try:
        record_audit(
            db,
            principal=principal,
            action="member.revoke",
            resource_type="project_member",
            resource_id=f"{principal_type}:{principal_id}",
            project_id=project_id,
            before={"role": row.role},
        )
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"revoked": True}


def revision(value: str | None) -> int:
    if value is None:
        raise DmrvError(428, "if_match_required", "Header If-Match wajib diisi; gunakan 0 untuk data baru.")
    if not re.fullmatch(r'"?\d+"?', value.strip()):
        raise DmrvError(400, "bad_if_match", "If-Match harus berupa nomor revisi.")
    return int(value.strip().strip('"'))


@router.get("/portfolio")
def get_portfolio(
    search: str = Query(default="", max_length=200),
    organization: str = Query(default="", max_length=255),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return integrity.portfolio(db, Principal.from_account(viewer), search, organization, limit, offset)


@router.get("/projects/{project_id}/integrity")
def get_workspace(project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return integrity.workspace(db, Principal.from_account(viewer), project_id)


@router.put("/projects/{project_id}/integrity/records/{category}/{key}", response_model=RecordOut)
def put_record(
    project_id: str,
    category: str,
    key: str,
    payload: RecordSave,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return integrity.save_record(
        db,
        Principal.from_account(viewer),
        project_id,
        category,
        key,
        payload,
        revision(if_match),
        getattr(request.state, "request_id", None),
    )


@router.post("/projects/{project_id}/integrity/records/{record_id}/review", response_model=RecordOut)
def review(
    project_id: str,
    record_id: str,
    payload: RecordReview,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return integrity.review_record(
        db,
        Principal.from_account(viewer),
        project_id,
        record_id,
        payload,
        revision(if_match),
        getattr(request.state, "request_id", None),
    )


@router.get("/projects/{project_id}/integrity/records/{record_id}", response_model=RecordOut)
def get_record(
    project_id: str, record_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    authorize(db, project_id, Principal.from_account(viewer), "accounting.read")
    return integrity.out(integrity._record(db, project_id, record_id))


@router.get("/projects/{project_id}/integrity/records/{record_id}/history")
def history(
    project_id: str, record_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    authorize(db, project_id, Principal.from_account(viewer), "accounting.read")
    integrity._record(db, project_id, record_id)
    rows = (
        db.execute(
            select(DmrvIntegrityRevision)
            .where(DmrvIntegrityRevision.record_id == record_id)
            .order_by(DmrvIntegrityRevision.revision.desc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        if hashlib.sha256(integrity.canonical(row.snapshot)).hexdigest() != row.sha256:
            raise DmrvError(409, "revision_integrity_failed", "Checksum riwayat penilaian tidak cocok.")
    return [{"revision": row.revision, "sha256": row.sha256, "snapshot": row.snapshot} for row in rows]


@router.put("/projects/{project_id}/integrity/risk-policy")
def put_policy(
    project_id: str,
    payload: RiskPolicyInput,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return save_policy(db, Principal.from_account(viewer), project_id, payload, revision(if_match))


@router.post("/projects/{project_id}/integrity/risk-scans", status_code=202)
def scan(
    project_id: str,
    payload: RiskScanInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    if not idempotency_key or len(idempotency_key) > 100:
        raise DmrvError(400, "idempotency_key_required", "Idempotency-Key wajib diisi (maks. 100 karakter).")
    row = enqueue_scan(db, Principal.from_account(viewer), project_id, payload.end_date, idempotency_key)
    return {"id": row.id, "kind": row.kind, "status": row.status, "project_id": project_id}


@router.post("/projects/{project_id}/integrity/validation-import", response_model=RecordOut)
async def import_validation(
    project_id: str,
    file: UploadFile = File(...),
    metadata: str = Form(...),
    key: str = Form(...),
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "monitoring.write")
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise DmrvError(413, "file_too_large", "CSV validasi maksimal 2 MB.")
    try:
        payload = json.loads(metadata)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if len(reader.fieldnames or []) != 5 or set(reader.fieldnames or []) != {
            "point_id",
            "lon",
            "lat",
            "observed",
            "predicted",
        }:
            raise ValueError("Kolom CSV: point_id,lon,lat,observed,predicted")
        pairs = []
        for line in reader:
            if len(pairs) >= 2000:
                raise ValueError("Maksimal 2000 titik per dataset.")
            pairs.append(line)
        title = payload.pop("title", "Validasi lapangan")
        payload["pairs"] = pairs
        parsed = RecordSave(title=title, payload=payload, status="draft")
    except (ValueError, UnicodeError, TypeError, AttributeError) as exc:
        raise DmrvError(422, "invalid_csv", str(exc)) from None
    return integrity.save_record(db, principal, project_id, "validation", key, parsed, revision(if_match))


@router.post("/projects/{project_id}/integrity/documents", status_code=201)
async def upload_document(
    project_id: str,
    file: UploadFile = File(...),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "monitoring.write")
    db.execute(select(DmrvProject.id).where(DmrvProject.id == project_id).with_for_update()).first()
    raw = await file.read(5 * 1024 * 1024 + 1)
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise DmrvError(413, "file_size", "Dokumen harus berisi data dan maksimal 5 MB.")
    filename = re.split(r"[/\\]", file.filename or "document")[-1]
    filename = re.sub(r"[^\w .()-]", "_", filename)[:200]
    ext = Path(filename).suffix.lower()
    types = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    valid = ext in types
    if ext == ".pdf":
        valid = raw.startswith(b"%PDF-")
    if ext == ".png":
        valid = raw.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {".jpg", ".jpeg"}:
        valid = raw.startswith(b"\xff\xd8\xff")
    if ext in {".docx", ".xlsx"}:
        valid = zipfile.is_zipfile(io.BytesIO(raw))
    if ext in {".txt", ".csv"}:
        try:
            raw.decode("utf-8-sig")
        except UnicodeError:
            valid = False
    if not valid:
        raise DmrvError(
            422, "unsupported_file", "Gunakan PDF, DOCX, XLSX, UTF-8 TXT/CSV, PNG, atau JPEG yang valid."
        )
    total = db.execute(
        select(func.coalesce(func.sum(DmrvIntegrityDocument.size_bytes), 0)).where(
            DmrvIntegrityDocument.project_id == project_id
        )
    ).scalar_one()
    if total + len(raw) > 50 * 1024 * 1024:
        raise DmrvError(413, "project_document_limit", "Batas dokumen proyek 50 MB tercapai.")
    row = DmrvIntegrityDocument(
        id=str(uuid.uuid4()),
        project_id=project_id,
        filename=filename,
        content_type=types[ext],
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        created_by=principal.key,
    )
    path = integrity.document_path(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(raw)
        db.add(row)
        db.flush()
        record_audit(
            db,
            principal=principal,
            action="integrity.document.upload",
            resource_type="integrity_document",
            resource_id=row.id,
            project_id=project_id,
            after={"filename": filename, "sha256": row.sha256, "size_bytes": len(raw)},
        )
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return integrity.document_out(row)


def _document(db: Session, principal: Principal, project_id: str, document_id: str):
    authorize(db, project_id, principal, "accounting.read")
    row = db.get(DmrvIntegrityDocument, document_id)
    if row is None or row.project_id != project_id:
        raise DmrvError(404, "not_found", "Dokumen tidak ditemukan.")
    path = integrity.document_path(row)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.sha256:
        raise DmrvError(409, "document_integrity_failed", "Dokumen hilang atau checksum tidak cocok.")
    return row, path


@router.get("/projects/{project_id}/integrity/documents/{document_id}/download")
def download_document(
    project_id: str, document_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    row, path = _document(db, Principal.from_account(viewer), project_id, document_id)
    return FileResponse(
        path,
        filename=row.filename,
        media_type=row.content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/projects/{project_id}/integrity/export")
def export_workspace(project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    principal = Principal.from_account(viewer)
    workspace = jsonable_encoder(integrity.workspace(db, principal, project_id, full=True))
    record_ids = [row["id"] for row in workspace["records"]]
    revisions = (
        db.execute(select(DmrvIntegrityRevision).where(DmrvIntegrityRevision.record_id.in_(record_ids)))
        .scalars()
        .all()
    )
    if any(hashlib.sha256(integrity.canonical(r.snapshot)).hexdigest() != r.sha256 for r in revisions):
        raise DmrvError(409, "revision_integrity_failed", "Checksum riwayat penilaian tidak cocok.")
    package = {
        "format": "savegeo_integrity_workspace_v1",
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "workspace": workspace,
        "history": [
            {"record_id": r.record_id, "revision": r.revision, "sha256": r.sha256, "snapshot": r.snapshot}
            for r in revisions
        ],
        "qualification": "Internal review package; no certification or issuance is implied.",
    }
    temp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    temp.close()
    target = Path(temp.name)
    try:
        manifest = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            metadata = integrity.canonical(package)
            archive.writestr("workspace.json", metadata)
            manifest.append({"path": "workspace.json", "sha256": hashlib.sha256(metadata).hexdigest()})
            for document in workspace["documents"]:
                row, path = _document(db, principal, project_id, document["id"])
                name = f"documents/{row.id}/{row.filename}"
                archive.write(path, name)
                manifest.append({"path": name, "sha256": row.sha256})
            archive.writestr(
                "manifest.json", integrity.canonical({"entries": manifest, "hash_algorithm": "sha256"})
            )
        return FileResponse(
            target,
            filename=f"savegeo-integrity-{project_id}.zip",
            media_type="application/zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise
