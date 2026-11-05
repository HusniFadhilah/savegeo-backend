"""dMRV API: /api/dmrv/projects. Thin routes; authorization lives in services/dmrv."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.core.security import admin_has_permission, get_current_app_viewer
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.dmrv import (
    BoundaryCreate,
    BoundaryOut,
    MemberAdd,
    MemberOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.services.dmrv import projects
from app.services.dmrv.authz import Principal
from app.services.dmrv.errors import DmrvError

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
