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
