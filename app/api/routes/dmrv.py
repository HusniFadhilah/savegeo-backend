"""dMRV API: /api/dmrv/projects. Thin routes; authorization lives in services/dmrv."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
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
from app.services.dmrv import field_data, projects
from app.services.dmrv.authz import Principal, authorize
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
