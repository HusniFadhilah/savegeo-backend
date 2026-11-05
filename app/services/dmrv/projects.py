"""dMRV project, membership and boundary operations.

Every function takes the SQLAlchemy session first, performs authorization,
changes state, writes the audit event in the same session and commits exactly
once. Any exception (including audit failure) leaves nothing committed.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_audit import DmrvIdempotencyKey
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.user import User
from app.schemas.dmrv import (
    BoundaryOut,
    MemberAdd,
    MemberOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import (
    PROJECT_ROLES,
    Principal,
    ProjectForbidden,
    ProjectNotFound,
    require_project_action,
)
from app.services.dmrv.boundary import BoundaryError, overlap_summary, validate_boundary
from app.services.dmrv.errors import DmrvError, project_not_found


def _authorize(db: Session, project_id: str, principal: Principal, action: str) -> str:
    try:
        return require_project_action(db, project_id, principal, action)
    except ProjectNotFound:
        raise project_not_found() from None
    except ProjectForbidden:
        raise DmrvError(403, "forbidden", "Peran Anda tidak memiliki izin untuk aksi ini.") from None


def _project_out(project: DmrvProject, role: str | None) -> ProjectOut:
    out = ProjectOut.model_validate(project, from_attributes=True)
    out.my_role = role
    return out


def _request_hash(payload: ProjectCreate) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _replay(db: Session, principal: Principal, key: str, request_hash: str) -> ProjectOut | None:
    row = db.execute(
        select(DmrvIdempotencyKey).where(
            DmrvIdempotencyKey.principal_key == principal.key, DmrvIdempotencyKey.key == key
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise DmrvError(422, "idempotency_key_reused", "Idempotency-Key sudah dipakai dengan isi permintaan berbeda.")
    return ProjectOut.model_validate(row.response_body)


def create_project(
    db: Session, principal: Principal, payload: ProjectCreate, idempotency_key: str, request_id: str | None
) -> tuple[ProjectOut, bool]:
    request_hash = _request_hash(payload)
    replayed = _replay(db, principal, idempotency_key, request_hash)
    if replayed is not None:
        return replayed, True

    project = DmrvProject(id=str(uuid.uuid4()), **payload.model_dump())
    try:
        db.add(project)
        db.flush()
        db.add(ProjectMember(
            project_id=project.id, principal_type=principal.type, principal_id=principal.id,
            role="project_owner", added_by=principal.key,
        ))
        out = _project_out(project, "project_owner")
        db.add(DmrvIdempotencyKey(
            principal_key=principal.key, key=idempotency_key, request_hash=request_hash,
            response_status=201, response_body=out.model_dump(mode="json"),
        ))
        record_audit(
            db, principal=principal, action="project.create", resource_type="project",
            resource_id=project.id, project_id=project.id,
            after=payload.model_dump(mode="json"), request_id=request_id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = _replay(db, principal, idempotency_key, request_hash)
        if concurrent is None:
            raise
        return concurrent, True
    except Exception:
        db.rollback()
        raise
    return out, False


def list_projects(db: Session, principal: Principal) -> list[ProjectOut]:
    rows = db.execute(
        select(DmrvProject, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == DmrvProject.id)
        .where(ProjectMember.principal_type == principal.type, ProjectMember.principal_id == principal.id)
        .order_by(DmrvProject.created_at.desc())
    ).all()
    return [_project_out(project, role) for project, role in rows]


def get_project(db: Session, principal: Principal, project_id: str) -> ProjectOut:
    role = _authorize(db, project_id, principal, "project.read")
    return _project_out(db.get(DmrvProject, project_id), role)


def update_project(
    db: Session, principal: Principal, project_id: str, payload: ProjectUpdate,
    if_match_revision: int, request_id: str | None,
) -> ProjectOut:
    role = _authorize(db, project_id, principal, "project.write")
    changes = payload.model_dump(exclude_unset=True)
    project = db.get(DmrvProject, project_id)
    before = {key: getattr(project, key) for key in changes}
    try:
        result = db.execute(
            update(DmrvProject)
            .where(DmrvProject.id == project_id, DmrvProject.revision == if_match_revision)
            .values(**changes, revision=if_match_revision + 1)
        )
        if result.rowcount != 1:
            raise DmrvError(412, "revision_mismatch", "Proyek sudah berubah sejak Anda memuatnya; muat ulang.")
        record_audit(
            db, principal=principal, action="project.update", resource_type="project",
            resource_id=project_id, project_id=project_id,
            before=json.loads(json.dumps(before, default=str)),
            after=json.loads(json.dumps(changes, default=str)), request_id=request_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.expire_all()
    return _project_out(db.get(DmrvProject, project_id), role)


def add_member(
    db: Session, principal: Principal, project_id: str, payload: MemberAdd, request_id: str | None
) -> MemberOut:
    _authorize(db, project_id, principal, "members.manage")
    if payload.role not in PROJECT_ROLES:
        raise DmrvError(422, "invalid_role", f"Peran harus salah satu dari: {', '.join(sorted(PROJECT_ROLES))}.")
    account_model: Any = User if payload.principal_type == "user" else AdminUser
    account = db.execute(select(account_model).where(account_model.username == payload.username)).scalar_one_or_none()
    if account is None:
        raise DmrvError(422, "unknown_account", "Akun tidak ditemukan.")
    exists = db.execute(
        select(ProjectMember.id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_type == payload.principal_type,
            ProjectMember.principal_id == account.id,
        )
    ).first()
    if exists:
        raise DmrvError(409, "already_member", "Akun sudah menjadi anggota proyek.")
    try:
        db.add(ProjectMember(
            project_id=project_id, principal_type=payload.principal_type, principal_id=account.id,
            role=payload.role, added_by=principal.key,
        ))
        record_audit(
            db, principal=principal, action="member.add", resource_type="project_member",
            resource_id=f"{payload.principal_type}:{account.id}", project_id=project_id,
            after={"principal": f"{payload.principal_type}:{account.id}", "role": payload.role}, request_id=request_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return MemberOut(principal_type=payload.principal_type, principal_id=account.id, username=payload.username, role=payload.role)


def _boundary_out(row: BoundaryVersion, *, with_geometry: bool, overlap: dict | None = None) -> BoundaryOut:
    return BoundaryOut(
        id=row.id, version=row.version, crs=row.crs, area_ha=row.area_ha, area_method=row.area_method,
        checksum=row.checksum, created_by=row.created_by, reason=row.reason, created_at=row.created_at,
        validated_geojson=row.validated_geojson if with_geometry else None, overlap=overlap,
    )


def _other_projects_latest_geometries(db: Session, project_id: str) -> list[dict]:
    latest = (
        select(BoundaryVersion.project_id, func.max(BoundaryVersion.version).label("v"))
        .where(BoundaryVersion.project_id != project_id)
        .group_by(BoundaryVersion.project_id)
        .subquery()
    )
    rows = db.execute(
        select(BoundaryVersion.validated_geojson).join(
            latest, (BoundaryVersion.project_id == latest.c.project_id) & (BoundaryVersion.version == latest.c.v)
        )
    ).scalars().all()
    return list(rows)


def add_boundary(
    db: Session, principal: Principal, project_id: str, geojson: dict, reason: str, request_id: str | None
) -> BoundaryOut:
    _authorize(db, project_id, principal, "boundary.write")
    try:
        validation = validate_boundary(geojson)
    except BoundaryError as exc:
        raise DmrvError(422, exc.code, str(exc)) from exc
    version = (db.execute(
        select(func.max(BoundaryVersion.version)).where(BoundaryVersion.project_id == project_id)
    ).scalar_one() or 0) + 1
    count, overlap_ha = overlap_summary(validation.validated_geojson, _other_projects_latest_geometries(db, project_id))
    row = BoundaryVersion(
        id=str(uuid.uuid4()), project_id=project_id, version=version, original_geojson=geojson,
        validated_geojson=validation.validated_geojson, area_ha=validation.area_ha,
        area_method=validation.area_method, checksum=validation.checksum, created_by=principal.key, reason=reason,
    )
    try:
        db.add(row)
        db.flush()
        record_audit(
            db, principal=principal, action="boundary.create", resource_type="boundary_version",
            resource_id=row.id, project_id=project_id, reason=reason, request_id=request_id,
            after={"version": version, "area_ha": validation.area_ha, "checksum": validation.checksum},
            snapshot_hash=validation.checksum,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DmrvError(409, "conflict", "Versi batas bentrok dengan permintaan lain; coba lagi.", retryable=True) from None
    except Exception:
        db.rollback()
        raise
    return _boundary_out(row, with_geometry=True, overlap={"other_projects": count, "overlap_ha": overlap_ha})


def list_boundaries(db: Session, principal: Principal, project_id: str) -> list[BoundaryOut]:
    _authorize(db, project_id, principal, "boundary.read")
    rows = db.execute(
        select(BoundaryVersion).where(BoundaryVersion.project_id == project_id).order_by(BoundaryVersion.version)
    ).scalars().all()
    return [_boundary_out(row, with_geometry=True) for row in rows]
