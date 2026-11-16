"""Customer workspace endpoints; no global role grants cross-tenant access."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.organizations import InvitationAccept, InvitationCreate, InvitationOut, MemberChange, OrganizationCreate, OrganizationOut
from app.services import organizations as service
from app.services.dmrv.authz import Principal
from app.services.dmrv.errors import DmrvError

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=list[OrganizationOut])
def list_organizations(viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return service.list_organizations(db, Principal.from_account(viewer))


@router.post("", response_model=OrganizationOut, status_code=201)
def create_organization(payload: OrganizationCreate, idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
                        viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    if not idempotency_key or len(idempotency_key) > 128:
        raise DmrvError(400, "idempotency_key_required", "Header Idempotency-Key (maks. 128 karakter) wajib diisi.")
    return service.create_organization(db, Principal.from_account(viewer), payload.name, idempotency_key)


@router.get("/{organization_id}/members")
def list_members(organization_id: str, viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return service.list_members(db, organization_id, Principal.from_account(viewer))


@router.patch("/{organization_id}/members/{member_id}")
def change_member(organization_id: str, member_id: str, payload: MemberChange,
                  viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return service.change_member(db, organization_id, member_id, Principal.from_account(viewer), payload)


@router.get("/{organization_id}/invitations", response_model=list[InvitationOut])
def list_invitations(organization_id: str, viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return service.list_invitations(db, organization_id, Principal.from_account(viewer))


@router.post("/{organization_id}/invitations", status_code=201)
def create_invitation(organization_id: str, payload: InvitationCreate, response: Response,
                      viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return service.create_invitation(db, organization_id, Principal.from_account(viewer), payload)


@router.delete("/{organization_id}/invitations/{invitation_id}", status_code=204)
def revoke_invitation(organization_id: str, invitation_id: str,
                      viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    service.revoke_invitation(db, organization_id, invitation_id, Principal.from_account(viewer))
    return Response(status_code=204)


@router.post("/invitations/accept", response_model=OrganizationOut)
def accept_invitation(payload: InvitationAccept, response: Response,
                      viewer: User | AdminUser = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return service.accept_invitation(db, Principal.from_account(viewer), payload.token, viewer.email)
