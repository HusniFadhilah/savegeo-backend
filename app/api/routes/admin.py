"""Admin API - auth, GEE credential management, system config, model registry CRUD.

Ports backend/admin_routes.py. Endpoints outside the required contract
(company-boundary CRUD, OpenRouter model proxy, AI key-pool status) were
intentionally left out of this pass - see migration summary.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.default_configs import is_sensitive_config_key
from app.core.security import (
    admin_has_permission,
    create_access_token,
    create_admin_password_reset_token,
    decode_admin_password_reset_token,
    get_current_admin,
    get_current_admin_panel,
    require_permission,
    ADMIN_SESSION_COOKIE,
    clear_session_cookie,
    hash_password,
    set_session_cookie,
    verify_password,
)
from app.db.models.admin_user import AdminUser
from app.db.models.company_boundary import CompanyBoundary
from app.db.models.gee_credential import GEECredential
from app.db.models.role import Permission, Role
from app.db.models.satellite_provider_entry import SatelliteProviderEntry
from app.db.models.system_config import SystemConfig
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal, get_db
from app.registries.satellite_provider_registry import SATELLITE_PROVIDERS
from app.repositories.satellite_provider_repo import apply_override, get_overrides_by_key
from app.repositories import imagery_provider_repo
from app.repositories.uploaded_model_repo import ALLOWED_MODEL_EXTS, import_legacy_models
from app.schemas.admin import (
    AdminProfileUpdateRequest,
    AdminUserCreateRequest,
    AdminUserUpdateRequest,
    ChangePasswordRequest,
    LoginRequest,
    ModelUpdateRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    SatelliteProviderUpdateRequest,
)
from app.services import audit_service, config_service, gee_service, storage_service
from app.services.imagery_provider_config import admin_connection_view, effective_settings, save_connection
from app.services.commercial_imagery_service import COMMERCIAL_PROVIDER_KEYS
from app.core.viewer_features import (
    FEATURE_ACCESS_DENIED_DETAIL,
    VIEWER_FEATURES,
    VIEWER_FEATURE_CODES,
    VIEWER_FEATURES_MANAGE_PERMISSION,
)
from app.services.arcgis_service import configure_arcgis_from_db
from app.services.datatable_service import datatables_response, is_datatables_request
from app.services.email_service import EmailDeliveryError, send_password_reset_email
from app.services.geo_utils import estimate_area_ha

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)
RESET_REQUEST_MESSAGE = (
    "Jika akun ditemukan, link reset password sudah dikirim ke email terdaftar."
)
_MODEL_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SENSITIVE_ROLE_PERMISSIONS = {"credential.read", "credential.write", "secret.read", "secret.write"}


def _is_sensitive_config_key(key: str) -> bool:
    return is_sensitive_config_key(key)


def _requires_full_access_config(key: str) -> bool:
    # ArcGIS portal settings can redirect privileged server-side requests, so
    # manage the whole integration alongside its API key.
    return _is_sensitive_config_key(key) or key.startswith("arcgis.")


def _visible_config_rows(rows: list[SystemConfig], admin: AdminUser) -> list[SystemConfig]:
    if admin_has_permission(admin, "secret.read"):
        return rows
    return [row for row in rows if not _requires_full_access_config(row.key)]


def _ensure_user_role_scope(admin: AdminUser, role: Role | None) -> None:
    """Prevent explicit admins from delegating or inheriting sensitive access."""
    if admin.role_id is None:
        return
    if role is None:
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat memberikan atau mengubah akun Full Access")
    if any(permission.code in _SENSITIVE_ROLE_PERMISSIONS for permission in role.permissions):
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat memberikan role yang mengakses credential atau secret")


def _safe_model_stem(value: str) -> str:
    """Return a filesystem-safe model stem without changing the DB display name."""
    return _MODEL_NAME_RE.sub("_", value).strip("._-") or "model"


# -- Auth --
@router.post("/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    admin = db.query(AdminUser).filter_by(username=payload.username).first()
    if not admin or not admin.is_active or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    admin.last_login = datetime.now(UTC)
    db.commit()
    set_session_cookie(response, ADMIN_SESSION_COOKIE, create_access_token(admin))
    return {"user": admin.to_dict()}


@router.post("/auth/logout")
def logout(response: Response):
    clear_session_cookie(response, ADMIN_SESSION_COOKIE)
    return {"message": "Logout berhasil"}


@router.get("/auth/me")
def me(admin: AdminUser = Depends(get_current_admin)):
    return admin.to_dict()


@router.put("/auth/profile")
def update_admin_profile(
    payload: AdminProfileUpdateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    db: Session = Depends(get_db),
):
    username = payload.username.strip()
    email = (payload.email or "").strip() or None
    if not username:
        raise HTTPException(status_code=400, detail="Nama pengguna wajib diisi")
    if len(username) > 64:
        raise HTTPException(status_code=400, detail="Nama pengguna maksimal 64 karakter")
    if email and (len(email) > 128 or "@" not in email or any(char.isspace() for char in email)):
        raise HTTPException(status_code=400, detail="Alamat email tidak valid")

    if db.query(AdminUser).filter(AdminUser.username == username, AdminUser.id != admin.id).first():
        raise HTTPException(status_code=409, detail="Nama pengguna sudah digunakan")
    if email and db.query(AdminUser).filter(AdminUser.email == email, AdminUser.id != admin.id).first():
        raise HTTPException(status_code=409, detail="Alamat email sudah digunakan")

    admin.username = username
    admin.email = email
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Nama pengguna atau email sudah digunakan") from exc
    db.refresh(admin)
    return {"message": "Profil berhasil diperbarui", "user": admin.to_dict()}


@router.put("/auth/change-password")
def change_password(
    payload: ChangePasswordRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="Old password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    admin.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"message": "Password updated"}


@router.post("/auth/forgot-password")
def forgot_password(payload: PasswordForgotRequest, db: Session = Depends(get_db)):
    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Email atau username wajib diisi")

    admin = (
        db.query(AdminUser)
        .filter((AdminUser.email == identifier) | (AdminUser.username == identifier))
        .first()
    )
    if not admin or not admin.is_active or not admin.email:
        return {"message": RESET_REQUEST_MESSAGE}

    settings = get_settings()
    token = create_admin_password_reset_token(admin)
    reset_url = f"{settings.frontend_base_url.rstrip('/')}/reset-password?token={token}"
    try:
        send_password_reset_email(admin.email, admin.username, reset_url)
    except EmailDeliveryError as exc:
        raise HTTPException(status_code=503, detail=f"Gagal mengirim email reset password: {exc}") from exc

    return {"message": RESET_REQUEST_MESSAGE}


@router.post("/auth/reset-password")
def reset_password(payload: PasswordResetRequest, db: Session = Depends(get_db)):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password baru minimal 8 karakter")
    reset_payload = decode_admin_password_reset_token(payload.token)
    admin = db.get(AdminUser, int(reset_payload["sub"]))
    if not admin or not admin.is_active:
        raise HTTPException(status_code=401, detail="Token reset password tidak valid")

    admin.password_hash = hash_password(payload.password)
    db.commit()
    return {"message": "Password berhasil diperbarui. Silakan login dengan password baru."}


# -- GEE credentials --
@router.get("/gee/credentials")
def list_gee_credentials(request: Request, admin: AdminUser = Depends(require_permission("credential.read")), db: Session = Depends(get_db)):
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
    notes: str | None = Form(None),
    activate: bool = Form(False),
    admin: AdminUser = Depends(require_permission("credential.write")),
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
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
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
def delete_gee_credential(cred_id: int, admin: AdminUser = Depends(require_permission("credential.write")), db: Session = Depends(get_db)):
    cred = db.get(GEECredential, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete the active credential - activate another one first")
    try:
        storage_service.delete_credential(cred.bucket_path)
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup; the DB row is still deleted below regardless
        logger.warning("Failed to delete GEE credential file %s from storage: %s", cred.bucket_path, type(exc).__name__)
    cred_id_val = cred.id
    db.delete(cred)
    db.commit()
    audit_service.log_audit(db, admin.id, "credential.delete", "gee_credential", cred_id_val)
    return {"message": "Credential deleted"}


@router.post("/gee/credentials/{cred_id}/activate")
def activate_gee_credential(cred_id: int, admin: AdminUser = Depends(require_permission("credential.write")), db: Session = Depends(get_db)):
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
def gee_status(request: Request, admin: AdminUser = Depends(require_permission("credential.read")), db: Session = Depends(get_db)):
    cred = gee_service.get_active_credential(db)
    return {
        "ee_initialized": getattr(request.app.state, "ee_initialized", False),
        "active_credential": cred.to_dict() if cred else None,
    }


@router.post("/gee/reinitialize")
def gee_reinitialize(request: Request, admin: AdminUser = Depends(require_permission("credential.write")), db: Session = Depends(get_db)):
    success = gee_service.initialize_ee(db)
    request.app.state.ee_initialized = success
    return {"success": success, "message": "Earth Engine reinitialized" if success else "Earth Engine initialization failed"}


# -- ArcGIS --
@router.get("/arcgis/status")
def arcgis_status(admin: AdminUser = Depends(require_permission("credential.read")), db: Session = Depends(get_db)):
    return configure_arcgis_from_db(db).get_status()


@router.put("/arcgis/config")
async def update_arcgis_config(
    request: Request,
    admin: AdminUser = Depends(require_permission("secret.write")),
    db: Session = Depends(get_db),
):
    """Save ArcGIS connection options and an optional write-only API key."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="ArcGIS settings must be a JSON object")

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be true or false")

    portal_url = str(body.get("portal_url", "")).strip().rstrip("/")
    parsed_url = urlparse(portal_url)
    if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise HTTPException(status_code=400, detail="portal_url must be an HTTP(S) portal base URL without credentials or query parameters")

    auth_mode = str(body.get("auth_mode", "")).strip().lower()
    if auth_mode not in {"none", "api_key"}:
        raise HTTPException(status_code=400, detail="auth_mode must be 'none' or 'api_key'")

    try:
        timeout = int(body.get("request_timeout", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="request_timeout must be an integer")
    if not 1 <= timeout <= 120:
        raise HTTPException(status_code=400, detail="request_timeout must be between 1 and 120 seconds")

    current_api_key = config_service.get_setting(db, "arcgis.api_key", get_settings().arcgis_api_key) or ""
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise HTTPException(status_code=400, detail="api_key must be a string")
    api_key = api_key.strip() if isinstance(api_key, str) else ""
    if auth_mode == "api_key" and not (api_key or current_api_key):
        raise HTTPException(status_code=400, detail="Enter an ArcGIS API key before selecting API key authentication")

    updates = [
        ("arcgis.enabled", enabled),
        ("arcgis.portal_url", portal_url),
        ("arcgis.auth_mode", auth_mode),
        ("arcgis.request_timeout", timeout),
    ]
    if api_key:
        updates.append(("arcgis.api_key", api_key))
    for key, value in updates:
        config_service.upsert_setting(db, key, value, updated_by=admin.id)

    client = configure_arcgis_from_db(db)
    audit_service.log_audit(db, admin.id, "arcgis.config.update", "arcgis_config", detail={"updated_keys": [key for key, _ in updates]})
    return {"message": "ArcGIS settings saved", "status": client.get_status()}


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
    category: str | None = None,
    admin: AdminUser = Depends(require_permission("config.read")),
    db: Session = Depends(get_db),
):
    rows = config_service.get_all_settings(db, category=category)
    grouped: dict[str, list] = {}
    for row in _visible_config_rows(rows, admin):
        grouped.setdefault(row.category, []).append(config_service.mask_config_dict(row))
    return {"config": grouped}


@router.get("/config/{category}")
def config_by_category(category: str, admin: AdminUser = Depends(require_permission("config.read")), db: Session = Depends(get_db)):
    rows = config_service.get_all_settings(db, category=category)
    return {"category": category, "config": [config_service.mask_config_dict(r) for r in _visible_config_rows(rows, admin)]}


@router.put("/config")
async def config_update(request: Request, admin: AdminUser = Depends(require_permission("config.write")), db: Session = Depends(get_db)):
    body = await request.json()
    if isinstance(body, dict) and "updates" in body and isinstance(body["updates"], list):
        items = [(u["key"], u.get("value")) for u in body["updates"]]
    else:
        items = [(k, v) for k, v in body.items()]

    updated = []
    errors = []
    for key, value in items:
        if _requires_full_access_config(key) and not admin_has_permission(admin, "secret.write"):
            errors.append({"key": key, "error": "Secret configuration is restricted to full-access administrators"})
            continue
        try:
            config_service.upsert_setting(db, key, value, updated_by=admin.id)
            updated.append(key)
        except Exception as exc:  # noqa: BLE001 - bulk update: one bad key shouldn't abort the rest, so it's collected
            errors.append({"key": key, "error": str(exc)})

    if updated:
        audit_service.log_audit(db, admin.id, "config.update", "system_config", detail={"updated_keys": updated})
        if any(key.startswith("arcgis.") for key in updated):
            configure_arcgis_from_db(db)
    return {"message": "Config updated", "updated": updated, "errors": errors}


@router.post("/config/reset")
def config_reset(
    payload: dict | None = None,
    admin: AdminUser = Depends(require_permission("config.write")),
    db: Session = Depends(get_db),
):
    key = (payload or {}).get("key")
    can_write_secrets = admin_has_permission(admin, "secret.write")
    if key is not None and _requires_full_access_config(key) and not can_write_secrets:
        raise HTTPException(status_code=403, detail="Secret configuration is restricted to full-access administrators")
    count = config_service.reset_setting(db, key, include_secrets=can_write_secrets)
    if key is None or key.startswith("arcgis."):
        configure_arcgis_from_db(db)
    return {"message": f"Reset {count} config value(s) to defaults"}


# -- Model manager --
@router.get("/models")
def admin_models_list(
    request: Request,
    model_type: str | None = None,
    admin: AdminUser = Depends(require_permission("model.read")),
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
    algorithm: str | None = Form(None),
    description: str | None = Form(None),
    version: str | None = Form(None),
    metrics: str | None = Form(None),
    feature_names: str | None = Form(None),
    metadata_json: str | None = Form(None),
    set_default: bool = Form(False),
    admin: AdminUser = Depends(require_permission("model.write")),
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
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = settings.model_path / f"{_safe_model_stem(name)}_{ts}{ext}"
    dest.write_bytes(raw)

    def _parse_json(s: str | None, default):
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
def admin_model_detail(model_id: int, admin: AdminUser = Depends(require_permission("model.read")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model.to_dict(include_path=True)


@router.put("/models/{model_id}")
def admin_model_update(
    model_id: int,
    payload: ModelUpdateRequest,
    admin: AdminUser = Depends(require_permission("model.write")),
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
def admin_model_delete(model_id: int, admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    if not model.is_legacy:
        try:
            Path(model.filepath).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup; the DB row is still deleted below regardless
            logger.warning("Failed to delete model file %s: %s", model.filepath, type(exc).__name__)
    model_id_val, model_name = model.id, model.name
    db.delete(model)
    db.commit()
    audit_service.log_audit(db, admin.id, "model.delete", "uploaded_model", model_id_val, detail={"name": model_name})
    return {"message": "Model deleted"}


@router.post("/models/{model_id}/set-default")
def admin_model_set_default(model_id: int, admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    db.query(UploadedModel).filter_by(model_type=model.model_type, is_default=True).update({"is_default": False})
    model.is_default = True
    db.commit()
    return {"message": f"{model.name!r} set as default {model.model_type} model"}


@router.post("/models/import-legacy")
def admin_import_legacy_models(admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    settings = get_settings()
    result = import_legacy_models(db, str(settings.model_path))
    return {"message": "Legacy import complete", **result}


# -- Admin users (RBAC) --
@router.get("/roles")
def list_roles(admin: AdminUser = Depends(require_permission("users.read")), db: Session = Depends(get_db)):
    roles = db.query(Role).order_by(Role.name).all()
    return {"roles": [r.to_dict() for r in roles]}


@router.get("/roles/viewer-features")
def get_viewer_feature_settings(
    admin: AdminUser = Depends(require_permission(VIEWER_FEATURES_MANAGE_PERMISSION)),
    db: Session = Depends(get_db),
):
    role = db.query(Role).filter(Role.name.ilike("viewer")).first()
    if role is None:
        raise HTTPException(status_code=404, detail="Viewer role is unavailable")
    enabled = {permission.code for permission in role.permissions if permission.code in VIEWER_FEATURE_CODES}
    return {
        "configured": role.feature_permissions_configured,
        "features": [
            {"code": code, "name": name, "description": description, "enabled": code in enabled}
            for code, name, description in VIEWER_FEATURES
        ],
    }


@router.put("/roles/viewer-features")
def update_viewer_feature_settings(
    payload: dict,
    admin: AdminUser = Depends(require_permission(VIEWER_FEATURES_MANAGE_PERMISSION)),
    db: Session = Depends(get_db),
):
    requested = payload.get("enabled_features")
    if not isinstance(requested, list) or any(not isinstance(code, str) for code in requested):
        raise HTTPException(status_code=422, detail="enabled_features must be a list of feature permission codes")
    enabled = set(requested)
    if len(enabled) != len(requested) or not enabled.issubset(VIEWER_FEATURE_CODES):
        raise HTTPException(status_code=422, detail="One or more feature permission codes are invalid")

    role = db.query(Role).filter(Role.name.ilike("viewer")).with_for_update().first()
    if role is None:
        raise HTTPException(status_code=404, detail="Viewer role is unavailable")
    permission_rows = {
        permission.code: permission
        for permission in db.query(Permission).filter(Permission.code.in_(VIEWER_FEATURE_CODES)).all()
    }
    if set(permission_rows) != VIEWER_FEATURE_CODES:
        raise HTTPException(status_code=503, detail=FEATURE_ACCESS_DENIED_DETAIL)
    preserved = [permission for permission in role.permissions if permission.code not in VIEWER_FEATURE_CODES]
    role.permissions = preserved + [permission_rows[code] for code, _, _ in VIEWER_FEATURES if code in enabled]
    role.feature_permissions_configured = True
    db.commit()
    audit_service.log_audit(
        db,
        admin.id,
        "role.viewer_features.update",
        "role",
        str(role.id),
        detail={"enabled_features": sorted(enabled)},
    )
    return {
        "configured": True,
        "features": [
            {"code": code, "name": name, "description": description, "enabled": code in enabled}
            for code, name, description in VIEWER_FEATURES
        ],
    }


@router.get("/users")
def admin_users_list(request: Request, admin: AdminUser = Depends(require_permission("users.read")), db: Session = Depends(get_db)):
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
    admin: AdminUser = Depends(require_permission("users.write")),
    db: Session = Depends(get_db),
):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.query(AdminUser).filter_by(username=payload.username).first():
        raise HTTPException(status_code=400, detail=f"Username {payload.username!r} already exists")
    assigned_role = db.get(Role, payload.role_id) if payload.role_id is not None else None
    if payload.role_id is not None and assigned_role is None:
        raise HTTPException(status_code=400, detail="role_id does not exist")
    _ensure_user_role_scope(admin, assigned_role)

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
    admin: AdminUser = Depends(require_permission("users.write")),
    db: Session = Depends(get_db),
):
    target = db.get(AdminUser, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if admin.role_id is not None and target.role_id is None:
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat mengubah akun Full Access")
    assigned_role = db.get(Role, payload.role_id) if payload.role_id is not None else None
    if payload.role_id is not None and assigned_role is None:
        raise HTTPException(status_code=400, detail="role_id does not exist")
    if payload.role_id is not None:
        _ensure_user_role_scope(admin, assigned_role)
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
def admin_user_delete(user_id: int, admin: AdminUser = Depends(require_permission("users.write")), db: Session = Depends(get_db)):
    target = db.get(AdminUser, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if admin.role_id is not None and target.role_id is None:
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat menghapus akun Full Access")
    if db.query(AdminUser).filter_by(is_active=True).count() <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last active admin account")

    username = target.username
    db.delete(target)
    db.commit()
    audit_service.log_audit(db, admin.id, "user.delete", "admin_user", user_id, detail={"username": username})
    return {"message": "User deleted"}


# -- Company boundaries (admin CRUD) --
INDUSTRY_TYPES = {"mining", "forestry", "plantation", "energy"}

# -- Company boundary import (OSM/GFW) - constants + helpers ported verbatim
#    from legacy backend/admin_routes.py --
_OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Area-like OSM objects only. The importer stores company boundaries/AOIs, so
# point-only company offices are intentionally not imported here.
_OSM_TAGS_MAP = {
    "mining": [
        '["landuse"="quarry"]',
        '["man_made"="mine"]',
        '["industrial"="mine"]',
        '["landuse"="industrial"]["industrial"="mine"]',
    ],
    "forestry": [
        '["landuse"="forest"]',
        '["boundary"="forest_compartment"]',
        '["landuse"="logging"]',
        '["industrial"="sawmill"]',
    ],
    "plantation": [
        '["landuse"="plantation"]',
        '["landuse"="farmland"]["crop"="palm_oil"]',
        '["landuse"="farmland"]["crop"="rubber"]',
        '["landuse"="farmland"]["crop"="cocoa"]',
        '["landuse"="farmland"]["crop"="coffee"]',
        '["landuse"="farmland"]["crop"="tea"]',
        '["landuse"="farmland"]["crop"="coconut"]',
        '["landuse"="farmland"]["crop"="sugarcane"]',
        '["landuse"="orchard"]',
    ],
    "energy": [
        '["power"="plant"]',
        '["power"="generator"]',
        '["landuse"="industrial"]["industrial"="oil"]',
        '["landuse"="industrial"]["industrial"="gas"]',
    ],
}

_COMPANY_IMPORT_WORKERS = max(1, int(os.getenv("COMPANY_IMPORT_WORKERS", "1")))
_company_import_executor = ThreadPoolExecutor(
    max_workers=_COMPANY_IMPORT_WORKERS,
    thread_name_prefix="company-import",
)
_company_import_slots = BoundedSemaphore(_COMPANY_IMPORT_WORKERS + 1)


def _company_import_jobs_dir() -> Path:
    path = Path(get_settings().upload_dir).resolve().parent / "company-import-jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _company_import_job_path(job_id: str) -> Path:
    try:
        safe_id = str(uuid.UUID(job_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="ID job impor tidak valid") from exc
    return _company_import_jobs_dir() / f"{safe_id}.json"


def _write_company_import_job(job_id: str, changes: dict) -> None:
    path = _company_import_job_path(job_id)
    current = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as stream:
            current = json.load(stream)
    data = {**current, **changes, "updated_at": datetime.now(UTC).isoformat()}
    descriptor, temporary_path = tempfile.mkstemp(prefix=f"{job_id}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _read_company_import_job(job_id: str) -> dict:
    path = _company_import_job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job impor tidak ditemukan")
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _queue_company_import(job_type: str, payload: dict, admin_id: int) -> dict:
    if not _company_import_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Impor perusahaan lain sedang diproses. Coba lagi sebentar.",
            headers={"Retry-After": "30"},
        )

    job_id = str(uuid.uuid4())
    try:
        _write_company_import_job(
            job_id,
            {
                "job_id": job_id,
                "type": job_type,
                "status": "queued",
                "message": "Impor masuk antrean.",
                "owner": admin_id,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        future = _company_import_executor.submit(_run_company_import, job_id, job_type, payload, admin_id)
        future.add_done_callback(lambda _future: _company_import_slots.release())
    except Exception:
        _company_import_slots.release()
        raise

    return {
        "job_id": job_id,
        "type": job_type,
        "status": "queued",
        "status_url": f"/admin/companies/import/jobs/{job_id}",
    }


def _osm_named_boundary_filters(industry_types: list[str]) -> list[str]:
    filters = []
    name_selectors = ('["name"]', '["operator"]', '["brand"]', '["ref"]')
    for itype in industry_types:
        for tag_selector in _OSM_TAGS_MAP.get(itype, []):
            for osm_type in ("way", "relation"):
                for name_selector in name_selectors:
                    filters.append(f"{osm_type}{tag_selector}{name_selector}(area.searchArea);")
    return sorted(set(filters))


def _classify_osm_industry(tags: dict) -> str:
    landuse = tags.get("landuse", "")
    industrial = tags.get("industrial", "")
    crop = tags.get("crop", "")
    if tags.get("power") in {"plant", "generator"} or industrial in {"oil", "gas"}:
        return "energy"
    if landuse == "quarry" or tags.get("man_made") == "mine" or industrial == "mine":
        return "mining"
    if landuse in {"plantation", "farmland", "orchard"} or crop:
        return "plantation"
    if landuse in {"forest", "logging"} or tags.get("boundary") == "forest_compartment" or industrial == "sawmill":
        return "forestry"
    return "mining"


def _osm_sub_type(tags: dict) -> str | None:
    for key in ("industrial", "crop", "power", "plant:source", "resource", "landuse", "boundary"):
        value = tags.get(key)
        if value:
            return str(value)
    return None


def _osm_element_to_geojson(element: dict) -> dict | None:
    etype = element.get("type")
    if etype == "way":
        geom = element.get("geometry", [])
        if not geom:
            return None
        coords = [[pt["lon"], pt["lat"]] for pt in geom]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        return {"type": "Polygon", "coordinates": [coords]} if len(coords) >= 4 else None
    if etype == "relation":
        outer_rings = []
        for member in element.get("members", []):
            if member.get("role") == "outer" and member.get("type") == "way":
                geom = member.get("geometry", [])
                if geom:
                    coords = [[pt["lon"], pt["lat"]] for pt in geom]
                    if coords[0] != coords[-1]:
                        coords.append(coords[0])
                    if len(coords) >= 4:
                        outer_rings.append(coords)
        if not outer_rings:
            return None
        if len(outer_rings) == 1:
            return {"type": "Polygon", "coordinates": outer_rings}
        return {"type": "MultiPolygon", "coordinates": [[ring] for ring in outer_rings]}
    return None


@router.get("/companies")
def admin_companies_list(
    request: Request,
    industry_type: str | None = None,
    province: str | None = None,
    search: str | None = None,
    admin: AdminUser = Depends(require_permission("company.read")),
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
def admin_companies_provinces(admin: AdminUser = Depends(require_permission("company.read")), db: Session = Depends(get_db)):
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
    admin: AdminUser = Depends(require_permission("company.write")),
    db: Session = Depends(get_db),
):
    content_type = request.headers.get("content-type", "")
    geojson_raw: str | None = None

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
    except Exception as exc:
        raise HTTPException(status_code=400, detail="GeoJSON tidak valid") from exc

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
def admin_company_detail(cid: int, admin: AdminUser = Depends(require_permission("company.read")), db: Session = Depends(get_db)):
    company = db.get(CompanyBoundary, cid)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company.to_dict(include_geojson=True)


@router.put("/companies/{cid}")
async def admin_company_update(
    cid: int,
    request: Request,
    admin: AdminUser = Depends(require_permission("company.write")),
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
def admin_company_delete(cid: int, admin: AdminUser = Depends(require_permission("company.write")), db: Session = Depends(get_db)):
    company = db.get(CompanyBoundary, cid)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    name = company.name
    db.delete(company)
    db.commit()
    audit_service.log_audit(db, admin.id, "company.delete", "company_boundary", cid, detail={"name": name})
    return {"message": "Dihapus"}


def _fetch_osm_overpass(overpass_query: str) -> tuple[dict | None, str]:
    """Fetch OSM data from Overpass mirrors. This runs in an import worker."""
    osm_data = None
    last_error = ""
    for server in _OVERPASS_SERVERS:
        try:
            resp = requests.post(
                server,
                data={"data": overpass_query},
                headers={"User-Agent": "SaveGeo company-boundary importer/1.0"},
                timeout=(15, 105),
            )
            if resp.status_code == 200:
                payload = resp.json()
                if payload.get("remark") and not payload.get("elements"):
                    last_error = str(payload["remark"])
                    continue
                osm_data = payload
                break
            last_error = f"HTTP {resp.status_code} dari {server}"
        except (requests.RequestException, ValueError) as e:
            last_error = f"{server}: {e}"
    return osm_data, last_error


def _perform_osm_company_import(db: Session, admin: AdminUser, industry_types: list[str], progress) -> dict:
    filters = _osm_named_boundary_filters(industry_types)
    if not filters:
        raise HTTPException(status_code=400, detail="Tidak ada tipe industri yang valid")

    overpass_query = (
        '[out:json][timeout:90][maxsize:536870912];\n'
        'area["ISO3166-1"="ID"][admin_level=2]->.searchArea;\n'
        f"({chr(10).join(filters)});\n"
        "out geom qt;"
    )

    progress("Mengambil data dari OpenStreetMap/Overpass...")
    osm_data, last_error = _fetch_osm_overpass(overpass_query)
    if osm_data is None:
        logger.warning("Overpass company import failed: %s", last_error)
        raise RuntimeError(f"Semua server Overpass gagal. {last_error[:300]} Coba lagi nanti.")

    elements = osm_data.get("elements", [])
    progress(f"Menyimpan {len(elements)} objek OSM ke basis data...")
    imported, skipped, errors = 0, 0, []
    for element in elements:
        tags = element.get("tags", {})
        name = tags.get("name") or tags.get("operator") or tags.get("ref")
        if not name:
            skipped += 1
            continue

        itype = _classify_osm_industry(tags)
        geom = _osm_element_to_geojson(element)
        if not geom:
            skipped += 1
            continue
        source_url = f"https://www.openstreetmap.org/{element.get('type')}/{element.get('id')}"
        if db.query(CompanyBoundary).filter_by(source="osm", source_url=source_url).first():
            skipped += 1
            continue

        feature = {"type": "Feature", "geometry": geom, "properties": {"name": name, "osm_id": element.get("id"), "tags": tags}}
        try:
            company = CompanyBoundary(
                name=name,
                company_name=tags.get("operator") or None,
                industry_type=itype,
                sub_type=_osm_sub_type(tags),
                province=tags.get("is_in:province") or tags.get("addr:province") or None,
                district=tags.get("addr:city") or tags.get("addr:district") or None,
                description=f"Diimpor dari OpenStreetMap. OSM ID: {element.get('id')}",
                geojson=feature,
                area_ha=estimate_area_ha(feature),
                source="osm",
                source_url=source_url,
                is_active=True,
            )
            db.add(company)
            imported += 1
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    db.commit()
    audit_service.log_audit(db, admin.id, "company.import_osm", "company_boundary", None, detail={"imported": imported, "skipped": skipped})
    return {"imported": imported, "skipped": skipped, "errors": errors[:10]}


@router.post("/companies/import/osm", status_code=202)
async def admin_companies_import_osm(
    request: Request,
    admin: AdminUser = Depends(require_permission("company.write")),
):
    """Queue an OSM import so Overpass latency cannot hold open the gateway request."""
    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body JSON tidak valid") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body harus berupa objek JSON")
    requested_types = data.get("industry_types") or list(INDUSTRY_TYPES)
    if not isinstance(requested_types, list):
        raise HTTPException(status_code=400, detail="industry_types harus berupa daftar")
    industry_types = list(
        dict.fromkeys(value for value in requested_types if isinstance(value, str) and value in INDUSTRY_TYPES)
    )
    if not industry_types:
        raise HTTPException(status_code=400, detail="Tidak ada tipe industri yang valid")
    return _queue_company_import("osm", {"industry_types": industry_types}, admin.id)


def _run_company_import(job_id: str, job_type: str, payload: dict, admin_id: int) -> None:
    _write_company_import_job(
        job_id,
        {"status": "running", "message": "Impor sedang diproses.", "started_at": datetime.now(UTC).isoformat()},
    )
    db = SessionLocal()
    try:
        admin = db.get(AdminUser, admin_id)
        if admin is None:
            raise RuntimeError("Akun administrator pemilik job tidak ditemukan.")

        def progress(message: str) -> None:
            _write_company_import_job(job_id, {"status": "running", "message": message})

        if job_type == "osm":
            result = _perform_osm_company_import(db, admin, payload["industry_types"], progress)
        else:
            raise RuntimeError("Jenis impor tidak dikenal.")
        _write_company_import_job(
            job_id,
            {
                "status": "completed",
                "message": "Impor selesai.",
                "result": result,
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - status is recorded for the polling client
        db.rollback()
        logger.exception("Company boundary %s import job %s failed", job_type, job_id)
        error = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _write_company_import_job(
            job_id,
            {
                "status": "failed",
                "message": "Impor gagal.",
                "error": str(error or "Kesalahan internal saat mengimpor.")[:600],
                "finished_at": datetime.now(UTC).isoformat(),
            },
        )
    finally:
        db.close()


@router.post("/companies/import/gfw")
async def admin_companies_import_gfw(
    _admin: AdminUser = Depends(require_permission("company.write")),
):
    """Reject the retired bulk importer with the actual WRI data restriction."""
    raise HTTPException(
        status_code=409,
        detail=(
            "Impor konsesi Indonesia dari GFW tidak tersedia: WRI membatasi dataset ini "
            "untuk tampilan saja dan tidak mengizinkan unduhan publik. Unggah GeoJSON "
            "yang Anda miliki hak aksesnya melalui formulir batas perusahaan."
        ),
    )


@router.get("/companies/import/jobs/{job_id}")
def admin_companies_import_job_status(
    job_id: str,
    admin: AdminUser = Depends(require_permission("company.write")),
):
    job = _read_company_import_job(job_id)
    if job.get("owner") != admin.id:
        raise HTTPException(status_code=404, detail="Job impor tidak ditemukan")
    return {key: value for key, value in job.items() if key != "owner"}


# -- Satellite provider overlay (admin CRUD on top of the static registry) --
@router.get("/satellite-providers")
def admin_satellite_providers_list(admin: AdminUser = Depends(require_permission("satellite.read")), db: Session = Depends(get_db)):
    """Merged registry+override view for the admin editor - unlike the public
    GET /vegetation/satellites picker, this includes admin-deactivated
    providers too (so there's something to toggle back on)."""
    overrides = get_overrides_by_key(db)
    out = []
    for key, meta in SATELLITE_PROVIDERS.items():
        override = overrides.get(key)
        merged = dict(apply_override(meta, override) or meta)
        merged["is_active"] = override.is_active if override else True
        merged["has_override"] = override is not None
        out.append(merged)
    return {"satellites": out}


@router.put("/satellite-providers/{key}")
def admin_satellite_provider_upsert(
    key: str,
    payload: SatelliteProviderUpdateRequest,
    admin: AdminUser = Depends(require_permission("satellite.write")),
    db: Session = Depends(get_db),
):
    if key not in SATELLITE_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider key '{key}' - harus salah satu dari {list(SATELLITE_PROVIDERS)}")
    row = db.query(SatelliteProviderEntry).filter_by(key=key).first()
    if row is None:
        row = SatelliteProviderEntry(key=key)
        db.add(row)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    audit_service.log_audit(db, admin.id, "satellite_provider.update", "satellite_provider", key, detail={"key": key})
    return {"message": "Provider satelit diperbarui", "provider": row.to_dict()}


@router.delete("/satellite-providers/{key}")
def admin_satellite_provider_reset(key: str, admin: AdminUser = Depends(require_permission("satellite.write")), db: Session = Depends(get_db)):
    """Remove the override row - reverts this provider back to its static registry default."""
    row = db.query(SatelliteProviderEntry).filter_by(key=key).first()
    if not row:
        raise HTTPException(status_code=404, detail="Tidak ada override untuk provider ini")
    db.delete(row)
    db.commit()
    audit_service.log_audit(db, admin.id, "satellite_provider.reset", "satellite_provider", key)
    return {"message": "Override dihapus, kembali ke default registry"}


# -- Raw imagery provider catalog and commercial connectors --
@router.get("/imagery-providers")
def admin_imagery_providers_list(
    admin: AdminUser = Depends(require_permission("satellite.read")), db: Session = Depends(get_db)
):
    settings = effective_settings(db)
    rows = imagery_provider_repo.list_provider_rows(db)
    for row in rows:
        row["connection"] = admin_connection_view(row["key"], settings)
        row["connection"]["can_write_credential"] = admin_has_permission(admin, "secret.write")
    return {"providers": rows}


@router.put("/imagery-providers/{key}")
async def admin_imagery_provider_update(
    key: str,
    request: Request,
    admin: AdminUser = Depends(require_permission("satellite.write")),
    db: Session = Depends(get_db),
):
    if key not in imagery_provider_repo.provider_catalog():
        raise HTTPException(status_code=404, detail="Provider citra tidak ditemukan")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Payload harus berupa objek")
    allowed = {
        "name", "provider", "group", "description", "resolution_m", "revisit_days", "start_year",
        "gee_collection", "cloud_property", "is_active", "display_order",
    }
    if set(payload) - allowed:
        raise HTTPException(status_code=422, detail="Ada field provider yang tidak dapat diedit")
    patch: dict = {}
    for field, value in payload.items():
        if field in {"is_active"}:
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail="is_active harus bernilai true atau false")
        elif field == "display_order":
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10000:
                raise HTTPException(status_code=422, detail="display_order harus berupa bilangan 0–10000")
        elif field in {"resolution_m", "revisit_days", "start_year"}:
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
                raise HTTPException(status_code=422, detail=f"{field} harus berupa bilangan positif")
        elif not isinstance(value, str) or len(value) > (10000 if field == "description" else 2048):
            raise HTTPException(status_code=422, detail=f"{field} tidak valid")
        patch[field] = value
    imagery_provider_repo.update_provider(db, key, patch)
    audit_service.log_audit(db, admin.id, "imagery_provider.update", "imagery_provider", key, detail={"fields": sorted(patch)})
    meta = imagery_provider_repo.list_provider_rows(db)
    provider = next(item for item in meta if item["key"] == key)
    provider["connection"] = admin_connection_view(key, effective_settings(db))
    provider["connection"]["can_write_credential"] = admin_has_permission(admin, "secret.write")
    return {"message": "Provider citra diperbarui", "provider": provider}


@router.put("/imagery-providers/{key}/connection")
async def admin_imagery_provider_connection_update(
    key: str,
    request: Request,
    admin: AdminUser = Depends(require_permission("satellite.write")),
    db: Session = Depends(get_db),
):
    if key not in COMMERCIAL_PROVIDER_KEYS:
        raise HTTPException(status_code=404, detail="Provider ini tidak memiliki konfigurasi koneksi komersial")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Payload harus berupa objek")
    if (payload.get("credential") or payload.get("clear_credential")) and not admin_has_permission(admin, "secret.write"):
        raise HTTPException(status_code=403, detail="Perubahan credential hanya dapat dilakukan administrator dengan secret.write")
    updated = save_connection(db, key, payload, admin.id)
    audit_service.log_audit(
        db, admin.id, "imagery_provider.connection_update", "imagery_provider", key,
        detail={
            "updated_keys": updated,
            "credential_updated": bool(payload.get("credential")),
            "credential_cleared": bool(payload.get("clear_credential")),
        },
    )
    return {"message": "Konfigurasi koneksi provider diperbarui", "updated": updated,
            "connection": {
                **admin_connection_view(key, effective_settings(db)),
                "can_write_credential": admin_has_permission(admin, "secret.write"),
            }}


@router.delete("/imagery-providers/{key}")
def admin_imagery_provider_reset(
    key: str,
    admin: AdminUser = Depends(require_permission("satellite.write")),
    db: Session = Depends(get_db),
):
    if key not in imagery_provider_repo.provider_catalog():
        raise HTTPException(status_code=404, detail="Provider citra tidak ditemukan")
    if not imagery_provider_repo.reset_provider(db, key):
        raise HTTPException(status_code=404, detail="Provider sudah menggunakan pengaturan katalog bawaan")
    audit_service.log_audit(db, admin.id, "imagery_provider.reset", "imagery_provider", key)
    return {"message": "Pengaturan provider dikembalikan ke katalog bawaan"}


# -- Key pool status (config-derived; savegeo/backend has no live LLM-call
#    rate-limit tracking like the legacy agentic_ai._KeyPool, so every
#    configured key reports available=True/available_in=0 rather than a
#    real cooldown - this endpoint's job here is just to show what backup
#    keys are configured per provider, not runtime rotation state). --
_KEY_POOL_PROVIDERS = ["gemini", "anthropic", "openai", "openrouter", "deepseek"]


def _mask_key_prefix(k: str) -> str:
    return f"{k[:6]}…{k[-4:]}" if len(k) > 10 else "***"


@router.get("/key-pool/status")
def key_pool_status(admin: AdminUser = Depends(require_permission("secret.read")), db: Session = Depends(get_db)):
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
def openrouter_models(free: str | None = None, q: str | None = None):
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
                raise HTTPException(status_code=502, detail="Model provider temporarily unavailable") from e
            # else: serve stale cache below

    models = _OR_MODEL_CACHE["data"] or []
    if free in ("1", "true", "yes"):
        models = [m for m in models if m["is_free"]]
    if q:
        needle = q.lower().strip()
        models = [m for m in models if needle in m["id"].lower() or needle in m["name"].lower()]

    return {"models": models, "total": len(models)}
