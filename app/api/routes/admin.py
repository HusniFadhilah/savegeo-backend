"""Admin API - auth, GEE credential management, system config, model registry CRUD.

Ports backend/admin_routes.py. Endpoints outside the required contract
(company-boundary CRUD, OpenRouter model proxy, AI key-pool status) were
intentionally left out of this pass - see migration summary.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import create_access_token, get_current_admin, hash_password, verify_password
from app.db.models.admin_user import AdminUser
from app.db.models.company_boundary import CompanyBoundary
from app.db.models.gee_credential import GEECredential
from app.db.models.role import Role
from app.db.models.system_config import SystemConfig
from app.db.models.uploaded_model import UploadedModel
from app.db.session import get_db
from app.repositories.uploaded_model_repo import ALLOWED_MODEL_EXTS, import_legacy_models
from app.schemas.admin import (
    AdminUserCreateRequest,
    AdminUserUpdateRequest,
    ChangePasswordRequest,
    LoginRequest,
    ModelUpdateRequest,
)
from app.services import audit_service, config_service, gee_service, storage_service
from app.services.geo_utils import estimate_area_ha
from app.services.datatable_service import datatables_response, is_datatables_request
from app.services.arcgis_service import get_arcgis_client

router = APIRouter(prefix="/admin", tags=["admin"])


# -- Auth --
@router.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter_by(username=payload.username).first()
    if not admin or not admin.is_active or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    admin.last_login = datetime.now(timezone.utc)
    db.commit()
    return {"token": create_access_token(admin), "user": admin.to_dict()}


@router.get("/auth/me")
def me(admin: AdminUser = Depends(get_current_admin)):
    return admin.to_dict()


@router.put("/auth/change-password")
def change_password(
    payload: ChangePasswordRequest,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="Old password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    admin.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password updated"}


# -- GEE credentials --
@router.get("/gee/credentials")
def list_gee_credentials(request: Request, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    query = db.query(GEECredential).order_by(GEECredential.uploaded_at.desc())

    if is_datatables_request(request):
        return datatables_response(
            request,
            query,
            row_mapper=lambda c: c.to_dict(),
            searchable_columns=[GEECredential.label, GEECredential.project_id, GEECredential.client_email],
        )

    creds = query.all()
    return {"credentials": [c.to_dict() for c in creds]}


@router.post("/gee/credentials", status_code=201)
async def upload_gee_credential(
    file: UploadFile = File(...),
    label: str = Form(...),
    notes: Optional[str] = Form(None),
    activate: bool = Form(False),
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Credential file too large (max 1MB)")
    try:
        key_data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    if key_data.get("type") != "service_account":
        raise HTTPException(status_code=400, detail="File is not a GEE service-account key (type != service_account)")
    required = ("type", "project_id", "private_key", "client_email")
    missing = [f for f in required if f not in key_data]
    if missing:
        raise HTTPException(status_code=400, detail=f"Key file missing required fields: {missing}")

    project_id = key_data["project_id"]
    client_email = key_data["client_email"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bucket_path = f"gee_{project_id}_{ts}.json"

    try:
        storage_service.upload_credential(bucket_path, raw)
    except storage_service.StorageNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if activate:
        db.query(GEECredential).update({GEECredential.is_active: False})

    cred = GEECredential(
        label=label,
        project_id=project_id,
        client_email=client_email,
        json_filename=file.filename or bucket_path,
        bucket_path=bucket_path,
        is_active=bool(activate),
        uploaded_by=admin.id,
        notes=notes,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    audit_service.log_audit(db, admin.id, "credential.create", "gee_credential", cred.id, detail={"label": label, "project_id": project_id})

    if activate:
        gee_service.initialize_ee(db)

    return {"message": "Credential uploaded", "credential": cred.to_dict()}


@router.delete("/gee/credentials/{cred_id}")
def delete_gee_credential(cred_id: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    cred = db.get(GEECredential, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete the active credential - activate another one first")
    try:
        storage_service.delete_credential(cred.bucket_path)
    except Exception:
        pass
    cred_id_val = cred.id
    db.delete(cred)
    db.commit()
    audit_service.log_audit(db, admin.id, "credential.delete", "gee_credential", cred_id_val)
    return {"message": "Credential deleted"}


@router.post("/gee/credentials/{cred_id}/activate")
def activate_gee_credential(cred_id: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    cred = db.get(GEECredential, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    db.query(GEECredential).update({GEECredential.is_active: False})
    cred.is_active = True
    db.commit()
    ee_initialized = gee_service.initialize_ee(db)
    audit_service.log_audit(db, admin.id, "credential.activate", "gee_credential", cred_id, detail={"ee_initialized": ee_initialized})
    return {"message": "Credential activated", "gee_initialized": ee_initialized}


@router.get("/gee/status")
def gee_status(request: Request, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    cred = gee_service.get_active_credential(db)
    return {
        "ee_initialized": getattr(request.app.state, "ee_initialized", False),
        "active_credential": cred.to_dict() if cred else None,
    }


@router.post("/gee/reinitialize")
def gee_reinitialize(request: Request, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    success = gee_service.initialize_ee(db)
    request.app.state.ee_initialized = success
    return {"success": success, "message": "Earth Engine reinitialized" if success else "Earth Engine initialization failed"}


# -- ArcGIS --
@router.get("/arcgis/status")
def arcgis_status(admin: AdminUser = Depends(get_current_admin)):
    return get_arcgis_client().get_status()


# -- System config --
# NOTE: unlike Flask/Werkzeug (which auto-prioritizes static path segments over
# variable ones regardless of registration order), FastAPI/Starlette matches
# routes strictly in registration order. "/config/public" and "/config" MUST be
# registered before the catch-all "/config/{category}", or requests to
# "/config/public" get routed there with category="public" and incorrectly
# require admin auth. (Found via live smoke-test against frontend-nextjs2.)
@router.get("/config/public")
def config_public(db: Session = Depends(get_db)):
    rows = db.query(SystemConfig).filter_by(is_public=True).all()
    return {row.key: row.typed_value() for row in rows}


@router.get("/config")
def config_all(
    category: Optional[str] = None,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    rows = config_service.get_all_settings(db, category=category)
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.category, []).append(config_service.mask_config_dict(row))
    return {"config": grouped}


@router.get("/config/{category}")
def config_by_category(category: str, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = config_service.get_all_settings(db, category=category)
    return {"category": category, "config": [config_service.mask_config_dict(r) for r in rows]}


@router.put("/config")
async def config_update(request: Request, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    body = await request.json()
    if isinstance(body, dict) and "updates" in body and isinstance(body["updates"], list):
        items = [(u["key"], u.get("value")) for u in body["updates"]]
    else:
        items = [(k, v) for k, v in body.items()]

    updated = []
    errors = []
    for key, value in items:
        try:
            config_service.upsert_setting(db, key, value, updated_by=admin.id)
            updated.append(key)
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})

    if updated:
        audit_service.log_audit(db, admin.id, "config.update", "system_config", detail={"updated_keys": updated})
    return {"message": "Config updated", "updated": updated, "errors": errors}


@router.post("/config/reset")
def config_reset(
    payload: dict | None = None,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    key = (payload or {}).get("key")
    count = config_service.reset_setting(db, key)
    return {"message": f"Reset {count} config value(s) to defaults"}


# -- Model manager --
@router.get("/models")
def admin_models_list(
    request: Request,
    model_type: Optional[str] = None,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    query = db.query(UploadedModel)
    if model_type:
        query = query.filter_by(model_type=model_type)

    if is_datatables_request(request):
        return datatables_response(
            request,
            query.order_by(UploadedModel.updated_at.desc()),
            row_mapper=lambda m: m.to_dict(),
            searchable_columns=[UploadedModel.name, UploadedModel.display_name, UploadedModel.algorithm, UploadedModel.model_type],
        )

    models = query.all()
    return {"models": [m.to_dict() for m in models]}


@router.post("/models/upload", status_code=201)
async def upload_model(
    file: UploadFile = File(...),
    name: str = Form(...),
    display_name: str = Form(...),
    model_type: str = Form(...),
    algorithm: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    version: Optional[str] = Form(None),
    metrics: Optional[str] = Form(None),
    feature_names: Optional[str] = Form(None),
    metadata_json: Optional[str] = Form(None),
    set_default: bool = Form(False),
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if model_type not in ("carbon", "vegetation", "landcover"):
        raise HTTPException(status_code=400, detail="model_type must be carbon, vegetation, or landcover")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_MODEL_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

    if db.query(UploadedModel).filter_by(name=name).first():
        raise HTTPException(status_code=400, detail=f"Model name {name!r} already exists")

    raw = await file.read()
    if len(raw) > 500 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Model file too large (max 500MB)")

    settings = get_settings()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = settings.model_path / f"{name}_{ts}{ext}"
    dest.write_bytes(raw)

    def _parse_json(s: Optional[str], default):
        if not s:
            return default
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return default

    if set_default:
        db.query(UploadedModel).filter_by(model_type=model_type, is_default=True).update({"is_default": False})

    model = UploadedModel(
        name=name,
        display_name=display_name,
        model_type=model_type,
        algorithm=algorithm,
        filename=dest.name,
        filepath=str(dest.resolve()),
        file_size_kb=round(len(raw) / 1024, 2),
        is_default=set_default,
        is_active=True,
        is_legacy=False,
        description=description,
        version=version,
        metrics=_parse_json(metrics, {}),
        feature_names=_parse_json(feature_names, []),
        metadata_json=_parse_json(metadata_json, {}),
        uploaded_by=admin.id,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    audit_service.log_audit(db, admin.id, "model.create", "uploaded_model", model.id, detail={"name": name, "model_type": model_type})
    return {"message": "Model uploaded", "model": model.to_dict(include_path=True)}


@router.get("/models/{model_id}")
def admin_model_detail(model_id: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model.to_dict(include_path=True)


@router.put("/models/{model_id}")
def admin_model_update(
    model_id: int,
    payload: ModelUpdateRequest,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(model, field, value)
    db.commit()
    db.refresh(model)
    return {"message": "Model updated", "model": model.to_dict(include_path=True)}


@router.delete("/models/{model_id}")
def admin_model_delete(model_id: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not model.is_legacy:
        try:
            Path(model.filepath).unlink(missing_ok=True)
        except Exception:
            pass
    model_id_val, model_name = model.id, model.name
    db.delete(model)
    db.commit()
    audit_service.log_audit(db, admin.id, "model.delete", "uploaded_model", model_id_val, detail={"name": model_name})
    return {"message": "Model deleted"}


@router.post("/models/{model_id}/set-default")
def admin_model_set_default(model_id: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    db.query(UploadedModel).filter_by(model_type=model.model_type, is_default=True).update({"is_default": False})
    model.is_default = True
    db.commit()
    return {"message": f"{model.name!r} set as default {model.model_type} model"}


@router.post("/models/import-legacy")
def admin_import_legacy_models(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    settings = get_settings()
    result = import_legacy_models(db, str(settings.model_path))
    return {"message": "Legacy import complete", **result}


# -- Admin users (RBAC) --
@router.get("/roles")
def list_roles(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    roles = db.query(Role).order_by(Role.name).all()
    return {"roles": [r.to_dict() for r in roles]}


@router.get("/users")
def admin_users_list(request: Request, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    query = db.query(AdminUser).order_by(AdminUser.created_at.desc())

    if is_datatables_request(request):
        return datatables_response(
            request,
            query,
            row_mapper=lambda u: u.to_dict(),
            searchable_columns=[AdminUser.username, AdminUser.email],
        )

    users = query.all()
    return {"users": [u.to_dict() for u in users]}


@router.post("/users", status_code=201)
def admin_user_create(
    payload: AdminUserCreateRequest,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.query(AdminUser).filter_by(username=payload.username).first():
        raise HTTPException(status_code=400, detail=f"Username {payload.username!r} already exists")
    if payload.role_id is not None and not db.get(Role, payload.role_id):
        raise HTTPException(status_code=400, detail="role_id does not exist")

    new_user = AdminUser(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        is_active=payload.is_active,
        role_id=payload.role_id,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    audit_service.log_audit(db, admin.id, "user.create", "admin_user", new_user.id, detail={"username": new_user.username})
    return {"message": "User created", "user": new_user.to_dict()}


@router.put("/users/{user_id}")
def admin_user_update(
    user_id: int,
    payload: AdminUserUpdateRequest,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    target = db.get(AdminUser, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.role_id is not None and not db.get(Role, payload.role_id):
        raise HTTPException(status_code=400, detail="role_id does not exist")
    if payload.is_active is False and target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    changes = {}
    if payload.email is not None:
        target.email = payload.email
        changes["email"] = payload.email
    if payload.is_active is not None:
        target.is_active = payload.is_active
        changes["is_active"] = payload.is_active
    if payload.role_id is not None:
        target.role_id = payload.role_id
        changes["role_id"] = payload.role_id
    if payload.new_password:
        if len(payload.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        target.password_hash = hash_password(payload.new_password)
        changes["password"] = "reset"

    db.commit()
    db.refresh(target)
    audit_service.log_audit(db, admin.id, "user.update", "admin_user", target.id, detail=changes)
    return {"message": "User updated", "user": target.to_dict()}


@router.delete("/users/{user_id}")
def admin_user_delete(user_id: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    target = db.get(AdminUser, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if db.query(AdminUser).filter_by(is_active=True).count() <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last active admin account")

    username = target.username
    db.delete(target)
    db.commit()
    audit_service.log_audit(db, admin.id, "user.delete", "admin_user", user_id, detail={"username": username})
    return {"message": "User deleted"}


# -- Company boundaries (admin CRUD) --
INDUSTRY_TYPES = {"mining", "forestry", "plantation", "energy"}


@router.get("/companies")
def admin_companies_list(
    request: Request,
    industry_type: Optional[str] = None,
    province: Optional[str] = None,
    search: Optional[str] = None,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    query = db.query(CompanyBoundary)
    if industry_type:
        query = query.filter_by(industry_type=industry_type)
    if province:
        query = query.filter_by(province=province)
    if search:
        query = query.filter(CompanyBoundary.name.ilike(f"%{search}%"))
    query = query.order_by(CompanyBoundary.name)

    if is_datatables_request(request):
        return datatables_response(
            request,
            query,
            row_mapper=lambda c: c.to_dict(),
            searchable_columns=[CompanyBoundary.name, CompanyBoundary.company_name, CompanyBoundary.province],
        )

    companies = query.all()
    return {"companies": [c.to_dict() for c in companies], "count": len(companies)}


@router.get("/companies/provinces")
def admin_companies_provinces(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = (
        db.query(CompanyBoundary.province)
        .filter(CompanyBoundary.province.isnot(None))
        .distinct()
        .order_by(CompanyBoundary.province)
        .all()
    )
    return {"provinces": [r[0] for r in rows if r[0]]}


@router.post("/companies", status_code=201)
async def admin_company_create(
    request: Request,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    content_type = request.headers.get("content-type", "")
    geojson_raw: Optional[str] = None

    if "multipart/form-data" in content_type:
        form = await request.form()
        name = str(form.get("name") or "").strip()
        industry_type = str(form.get("industry_type") or "").strip()
        company_name = str(form.get("company_name") or "").strip()
        sub_type = str(form.get("sub_type") or "").strip()
        province = str(form.get("province") or "").strip()
        district = str(form.get("district") or "").strip()
        description = str(form.get("description") or "").strip()
        geojson_file = form.get("geojson_file")
        if geojson_file is not None and hasattr(geojson_file, "read"):
            geojson_raw = (await geojson_file.read()).decode("utf-8")
        elif form.get("geojson_text"):
            geojson_raw = str(form.get("geojson_text"))
    else:
        data = await request.json()
        name = str(data.get("name") or "").strip()
        industry_type = str(data.get("industry_type") or "").strip()
        company_name = str(data.get("company_name") or "").strip()
        sub_type = str(data.get("sub_type") or "").strip()
        province = str(data.get("province") or "").strip()
        district = str(data.get("district") or "").strip()
        description = str(data.get("description") or "").strip()
        raw_gj = data.get("geojson")
        geojson_raw = json.dumps(raw_gj) if raw_gj else (data.get("geojson_text") or None)

    if not name:
        raise HTTPException(status_code=400, detail="name diperlukan")
    if industry_type not in INDUSTRY_TYPES:
        raise HTTPException(status_code=400, detail=f"industry_type harus salah satu dari {sorted(INDUSTRY_TYPES)}")
    if not geojson_raw:
        raise HTTPException(status_code=400, detail="geojson diperlukan (file atau teks)")

    try:
        gj = json.loads(geojson_raw) if isinstance(geojson_raw, str) else geojson_raw
        if gj.get("type") not in ("Feature", "FeatureCollection", "Polygon", "MultiPolygon", "GeometryCollection"):
            raise ValueError("unknown geojson type")
    except Exception:
        raise HTTPException(status_code=400, detail="GeoJSON tidak valid")

    company = CompanyBoundary(
        name=name,
        company_name=company_name or None,
        industry_type=industry_type,
        sub_type=sub_type or None,
        province=province or None,
        district=district or None,
        description=description or None,
        geojson=gj,
        area_ha=estimate_area_ha(gj),
        source="manual",
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    audit_service.log_audit(db, admin.id, "company.create", "company_boundary", company.id, detail={"name": name})
    return {"message": "Company boundary berhasil disimpan", "company": company.to_dict()}


@router.get("/companies/{cid}")
def admin_company_detail(cid: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    company = db.get(CompanyBoundary, cid)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.to_dict(include_geojson=True)


@router.put("/companies/{cid}")
async def admin_company_update(
    cid: int,
    request: Request,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    company = db.get(CompanyBoundary, cid)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    data = await request.json()
    for field in ("name", "company_name", "industry_type", "sub_type", "province", "district", "description"):
        if field in data:
            setattr(company, field, data[field])
    if "is_active" in data:
        company.is_active = bool(data["is_active"])
    if "geojson" in data:
        raw = data["geojson"]
        gj = raw if isinstance(raw, dict) else json.loads(raw)
        company.geojson = gj
        company.area_ha = estimate_area_ha(gj)
    db.commit()
    db.refresh(company)
    audit_service.log_audit(db, admin.id, "company.update", "company_boundary", cid)
    return {"message": "Diperbarui", "company": company.to_dict()}


@router.delete("/companies/{cid}")
def admin_company_delete(cid: int, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    company = db.get(CompanyBoundary, cid)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    name = company.name
    db.delete(company)
    db.commit()
    audit_service.log_audit(db, admin.id, "company.delete", "company_boundary", cid, detail={"name": name})
    return {"message": "Dihapus"}


# -- Key pool status (config-derived; savegeo/backend has no live LLM-call
#    rate-limit tracking like the legacy agentic_ai._KeyPool, so every
#    configured key reports available=True/available_in=0 rather than a
#    real cooldown - this endpoint's job here is just to show what backup
#    keys are configured per provider, not runtime rotation state). --
_KEY_POOL_PROVIDERS = ["gemini", "anthropic", "openai", "openrouter", "deepseek"]


def _mask_key_prefix(k: str) -> str:
    return f"{k[:6]}…{k[-4:]}" if len(k) > 10 else "***"


@router.get("/key-pool/status")
def key_pool_status(admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    result: dict[str, list[dict]] = {}
    for provider in _KEY_POOL_PROVIDERS:
        primary = config_service.get_setting(db, f"ai.{provider}_api_key", "") or ""
        backup_raw = config_service.get_setting(db, f"ai.backup_keys.{provider}", "") or ""
        backups = [k.strip() for k in backup_raw.splitlines() if k.strip()]
        seen: set[str] = set()
        keys = []
        for k in [primary.strip(), *backups]:
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        if keys:
            result[provider] = [{"prefix": _mask_key_prefix(k), "available": True, "available_in": 0} for k in keys]
    return result


# -- OpenRouter model catalogue proxy (public API, cached 5 min in-process) --
_OR_MODEL_CACHE: dict = {"data": None, "ts": 0.0}
_OR_CACHE_TTL = 300.0


@router.get("/openrouter/models")
def openrouter_models(free: Optional[str] = None, q: Optional[str] = None):
    import time as _time

    now = _time.time()
    if not _OR_MODEL_CACHE["data"] or now - _OR_MODEL_CACHE["ts"] > _OR_CACHE_TTL:
        try:
            import requests

            r = requests.get(
                "https://openrouter.ai/api/v1/models",
                timeout=10,
                headers={"User-Agent": "SaveGeo/1.0"},
            )
            r.raise_for_status()
            raw = r.json().get("data", [])
            models = []
            for m in raw:
                pricing = m.get("pricing", {})
                inp = float(pricing.get("prompt", "0") or 0)
                out = float(pricing.get("completion", "0") or 0)
                models.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", m.get("id", "")),
                    "is_free": inp == 0 and out == 0,
                    "context_length": m.get("context_length", 0),
                    "input_per_m": round(inp * 1_000_000, 4),
                    "output_per_m": round(out * 1_000_000, 4),
                })
            _OR_MODEL_CACHE["data"] = models
            _OR_MODEL_CACHE["ts"] = now
        except Exception as e:  # noqa: BLE001
            if not _OR_MODEL_CACHE["data"]:
                raise HTTPException(status_code=502, detail=str(e))
            # else: serve stale cache below

    models = _OR_MODEL_CACHE["data"] or []
    if free in ("1", "true", "yes"):
        models = [m for m in models if m["is_free"]]
    if q:
        needle = q.lower().strip()
        models = [m for m in models if needle in m["id"].lower() or needle in m["name"].lower()]

    return {"models": models, "total": len(models)}
