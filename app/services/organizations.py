"""Transactional organization membership and scoped invitation acceptance.

Every change locks the organization row before inspecting its membership so two
owners cannot concurrently remove the final owner. No global-admin bypass.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_project import DmrvProject
from app.db.models.organization import Organization, OrganizationInvitation, OrganizationMember, WorkspaceRequest
from app.db.models.user import User
from app.schemas.organizations import InvitationCreate, InvitationOut, MemberChange, OrganizationOut
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal
from app.services.dmrv.errors import DmrvError


def _missing() -> DmrvError:
    return DmrvError(404, "organization_not_found", "Organisasi tidak ditemukan.")


def membership(db: Session, organization_id: str, principal: Principal) -> OrganizationMember | None:
    return db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == organization_id,
        OrganizationMember.principal_type == principal.type,
        OrganizationMember.principal_id == principal.id,
        OrganizationMember.is_active.is_(True),
    ))


def require_member(db: Session, organization_id: str, principal: Principal, *, manage: bool = False) -> OrganizationMember:
    member = membership(db, organization_id, principal)
    if member is None:
        raise _missing()
    if manage and member.role not in {"owner", "admin"}:
        raise DmrvError(403, "organization_forbidden", "Hanya pengelola organisasi yang dapat melakukan tindakan ini.")
    return member


def _lock(db: Session, organization_id: str) -> Organization:
    org = db.scalar(select(Organization).where(Organization.id == organization_id).with_for_update())
    if org is None:
        raise _missing()
    return org


def _out(org: Organization, role: str) -> OrganizationOut:
    return OrganizationOut(id=org.id, name=org.name, role=role, revision=org.revision, personal=org.personal_owner is not None)


def _audit(db: Session, principal: Principal, org_id: str, action: str, after: dict) -> None:
    record_audit(db, principal=principal, action=action, resource_type="organization", resource_id=org_id,
                 project_id=None, after=after)


def list_organizations(db: Session, principal: Principal) -> list[OrganizationOut]:
    rows = db.execute(select(Organization, OrganizationMember.role).join(
        OrganizationMember, OrganizationMember.organization_id == Organization.id,
    ).where(OrganizationMember.principal_type == principal.type,
            OrganizationMember.principal_id == principal.id, OrganizationMember.is_active.is_(True))
        .order_by(Organization.created_at, Organization.id)).all()
    return [_out(org, role) for org, role in rows]


def create_organization(db: Session, principal: Principal, name: str, key: str) -> OrganizationOut:
    digest = hashlib.sha256(json.dumps({"kind": "organization.create", "name": name}, sort_keys=True).encode()).hexdigest()

    def replay() -> OrganizationOut | None:
        prior = db.scalar(select(WorkspaceRequest).where(WorkspaceRequest.principal_key == principal.key, WorkspaceRequest.key == key))
        if prior is None:
            return None
        if prior.request_hash != digest:
            raise DmrvError(409, "idempotency_key_reused", "Kunci permintaan telah digunakan untuk isi yang berbeda.")
        require_member(db, prior.response_body["id"], principal)
        return OrganizationOut.model_validate(prior.response_body)

    prior = replay()
    if prior:
        return prior
    org = Organization(id=str(uuid.uuid4()), name=name, created_by=principal.key)
    try:
        db.add(org)
        db.flush()
        db.add(OrganizationMember(id=str(uuid.uuid4()), organization_id=org.id, principal_type=principal.type,
                                  principal_id=principal.id, role="owner", is_active=True))
        out = _out(org, "owner")
        db.add(WorkspaceRequest(id=str(uuid.uuid4()), principal_key=principal.key, key=key,
                                request_hash=digest, response_body=out.model_dump(mode="json")))
        _audit(db, principal, org.id, "organization.create", {"name": name})
        db.commit()
        return out
    except IntegrityError:
        db.rollback()
        prior = replay()
        if prior:
            return prior
        raise
    except Exception:
        db.rollback()
        raise


def list_members(db: Session, org_id: str, principal: Principal) -> list[dict]:
    require_member(db, org_id, principal, manage=True)
    rows = db.scalars(select(OrganizationMember).where(OrganizationMember.organization_id == org_id)
                      .order_by(OrganizationMember.created_at, OrganizationMember.id)).all()
    result = []
    for member in rows:
        account = db.get(User if member.principal_type == "user" else AdminUser, member.principal_id)
        result.append({"id": member.id, "principal_type": member.principal_type, "username": account.username if account else None,
                       "full_name": account.full_name if account else None, "role": member.role, "is_active": member.is_active})
    return result


def change_member(db: Session, org_id: str, member_id: str, principal: Principal, change: MemberChange) -> dict:
    try:
        _lock(db, org_id)
        actor = require_member(db, org_id, principal, manage=True)
        target = db.scalar(select(OrganizationMember).where(OrganizationMember.id == member_id,
                                                          OrganizationMember.organization_id == org_id))
        if target is None:
            raise _missing()
        if actor.role != "owner" and (target.role in {"owner", "admin"} or change.role != "member"):
            raise DmrvError(403, "owner_required", "Perubahan pengelola hanya dapat dilakukan pemilik organisasi.")
        if target.role == "owner" and target.is_active and (change.role != "owner" or not change.is_active):
            owners = db.scalar(select(func.count()).select_from(OrganizationMember).where(
                OrganizationMember.organization_id == org_id, OrganizationMember.role == "owner", OrganizationMember.is_active.is_(True)))
            if owners <= 1:
                raise DmrvError(409, "last_owner", "Tetapkan pemilik pengganti sebelum menonaktifkan atau menurunkan pemilik terakhir.")
        target.role, target.is_active = change.role, change.is_active
        _audit(db, principal, org_id, "organization.member.update", {"member_id": member_id, **change.model_dump()})
        db.commit()
        return {"id": target.id, "role": target.role, "is_active": target.is_active}
    except Exception:
        db.rollback()
        raise


def create_invitation(db: Session, org_id: str, principal: Principal, payload: InvitationCreate) -> dict:
    try:
        _lock(db, org_id)
        actor = require_member(db, org_id, principal, manage=True)
        if payload.role == "admin" and actor.role != "owner":
            raise DmrvError(403, "owner_required", "Undangan pengelola memerlukan peran pemilik.")
        token = secrets.token_urlsafe(32)
        invite = OrganizationInvitation(id=str(uuid.uuid4()), organization_id=org_id, email=payload.email,
            role=payload.role, token_hash=hashlib.sha256(token.encode()).hexdigest(), created_by=principal.key,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=payload.expires_in_hours))
        db.add(invite)
        _audit(db, principal, org_id, "organization.invitation.create", {"invitation_id": invite.id, "role": payload.role})
        db.commit()
        # Returned exactly once for controlled delivery; never persisted in audit/history.
        return {"invitation": InvitationOut.model_validate(invite, from_attributes=True).model_dump(mode="json"), "token": token}
    except Exception:
        db.rollback()
        raise


def list_invitations(db: Session, org_id: str, principal: Principal) -> list[InvitationOut]:
    require_member(db, org_id, principal, manage=True)
    return [InvitationOut.model_validate(row, from_attributes=True) for row in db.scalars(
        select(OrganizationInvitation).where(OrganizationInvitation.organization_id == org_id)
        .order_by(OrganizationInvitation.created_at.desc()).limit(100))]


def revoke_invitation(db: Session, org_id: str, invite_id: str, principal: Principal) -> None:
    try:
        _lock(db, org_id)
        require_member(db, org_id, principal, manage=True)
        row = db.scalar(select(OrganizationInvitation).where(OrganizationInvitation.id == invite_id,
                                                           OrganizationInvitation.organization_id == org_id))
        if row is None:
            raise _missing()
        row.revoked_at = dt.datetime.now(dt.UTC)
        _audit(db, principal, org_id, "organization.invitation.revoke", {"invitation_id": row.id})
        db.commit()
    except Exception:
        db.rollback()
        raise


def accept_invitation(db: Session, principal: Principal, token: str, email: str | None) -> OrganizationOut:
    hashed = hashlib.sha256(token.encode()).hexdigest()
    row = db.scalar(select(OrganizationInvitation).where(OrganizationInvitation.token_hash == hashed))
    if row is None:
        raise DmrvError(404, "invitation_not_found", "Undangan tidak tersedia.")
    org_id = row.organization_id
    try:
        org = _lock(db, org_id)
        # Reload after obtaining the common lock, including when another request consumed it.
        db.refresh(row)
        expiry = row.expires_at.replace(tzinfo=dt.UTC) if row.expires_at.tzinfo is None else row.expires_at
        if row.accepted_at or row.revoked_at or expiry <= dt.datetime.now(dt.UTC) or not email or email.casefold() != row.email:
            raise DmrvError(404, "invitation_not_found", "Undangan tidak tersedia untuk akun ini.")
        existing = db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == org_id,
            OrganizationMember.principal_type == principal.type, OrganizationMember.principal_id == principal.id))
        if existing:
            raise DmrvError(409, "already_member", "Keanggotaan sudah tercatat; perubahan akses dilakukan oleh pemilik.")
        db.add(OrganizationMember(id=str(uuid.uuid4()), organization_id=org_id, principal_type=principal.type,
                                  principal_id=principal.id, role=row.role, is_active=True))
        row.accepted_at = dt.datetime.now(dt.UTC)
        _audit(db, principal, org_id, "organization.invitation.accept", {"invitation_id": row.id})
        db.commit()
        return _out(org, row.role)
    except Exception:
        db.rollback()
        raise


def filter_projects(db: Session, principal: Principal, projects: list[tuple]) -> list[tuple]:
    """One membership query for a portfolio, without touching legacy projects."""
    org_ids = {project.organization_id for project, _ in projects if project.organization_id}
    if not org_ids:
        return projects
    allowed = set(db.scalars(select(OrganizationMember.organization_id).where(
        OrganizationMember.organization_id.in_(org_ids), OrganizationMember.principal_type == principal.type,
        OrganizationMember.principal_id == principal.id, OrganizationMember.is_active.is_(True))))
    return [(project, role) for project, role in projects if project.organization_id is None or project.organization_id in allowed]


def require_project_organization(db: Session, project: DmrvProject, principal: Principal) -> None:
    if project.organization_id:
        require_member(db, project.organization_id, principal)
