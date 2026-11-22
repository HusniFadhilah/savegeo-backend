"""Admin API - auth, GEE credential management, system config, model registry CRUD.

Ports backend/admin_routes.py. Endpoints outside the required contract
(company-boundary CRUD, OpenRouter model proxy, AI key-pool status) were
intentionally left out of this pass - see migration summary.
"""
from __future__ import annotations

import json
import csv
import hashlib
import io
import logging
import math
import os
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import MetaData, Table, inspect as sa_inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import auth_throttle
from app.core.public_http import public_http, requests as safe_requests
from app.core.config import get_settings
from app.core.bounded_json import read_bounded_json_object
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
    verify_login_password,
    verify_password,
    ensure_current_session,
    revoke_account_sessions,
    require_recent_mfa,
)
from app.db.models.admin_user import AdminUser
from app.db.models.audit_log import AuditLog
from app.db.models.company_boundary import CompanyBoundary
from app.db.models.gee_credential import GEECredential
from app.db.models.role import Permission, Role
from app.db.models.satellite_provider_entry import SatelliteProviderEntry
from app.db.models.system_config import SystemConfig
from app.db.models.uploaded_model import UploadedModel, UploadedModelEvaluation
from app.db.session import SessionLocal, get_db
from app.registries.satellite_provider_registry import SATELLITE_PROVIDERS
from app.repositories.satellite_provider_repo import apply_override, get_overrides_by_key
from app.repositories import imagery_provider_repo
from app.repositories.uploaded_model_repo import ALLOWED_MODEL_EXTS, import_legacy_models
from app.schemas.admin import (
    AdminNameUpdateRequest,
    AdminProfileUpdateRequest,
    AdminUserCreateRequest,
    AdminUserUpdateRequest,
    ChangePasswordRequest,
    LoginRequest,
    ModelUpdateRequest,
    ModelReviewRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    SatelliteProviderUpdateRequest,
)
from app.services import audit_service, config_service, gee_service, storage_service, model_lifecycle
from app.services import model_evaluation_storage
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


def _role_is_sensitive(role: Role) -> bool:
    return any(permission.code in _SENSITIVE_ROLE_PERMISSIONS for permission in role.permissions)


def _ensure_user_role_scope(admin: AdminUser, role: Role | None) -> None:
    """Prevent explicit admins from delegating or inheriting sensitive access."""
    if admin.is_superuser:
        return
    if role is None:
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat memberikan atau mengubah akun Full Access")
    if _role_is_sensitive(role):
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat memberikan role yang mengakses credential atau secret")


def _safe_model_stem(value: str) -> str:
    """Return a filesystem-safe model stem without changing the DB display name."""
    return _MODEL_NAME_RE.sub("_", value).strip("._-") or "model"


def _ensure_model_execution_permission(admin: AdminUser) -> None:
    # Serialized ML artifacts can execute code when deserialized. Upload/import
    # is a server-code trust decision, beyond ordinary model catalog editing.
    if not admin_has_permission(admin, "secret.write"):
        raise HTTPException(status_code=403, detail="Model upload/import requires trusted server execution access")


# -- Auth --
@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    auth_throttle.ensure_login_allowed(request, "admin", payload.username)
    admin = db.query(AdminUser).filter_by(username=payload.username).first()
    if not verify_login_password(payload.password, admin):
        auth_throttle.record_login_failure(request, "admin", payload.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    auth_throttle.record_login_success(request, "admin", payload.username)
    from app.core.security import requires_admin_mfa
    if requires_admin_mfa(admin):
        from app.services import admin_mfa
        from app.core.secret_encryption import SecretEncryptionError
        try:
            challenge = admin_mfa.begin(db, admin)
        except SecretEncryptionError:
            raise HTTPException(503, "Konfigurasi enkripsi MFA server belum tersedia.") from None
        clear_session_cookie(response, ADMIN_SESSION_COOKIE)
        response.headers["Cache-Control"] = "no-store"
        return challenge
    admin.last_login = datetime.now(UTC)
    db.commit()
    set_session_cookie(response, ADMIN_SESSION_COOKIE, create_access_token(admin))
    return {"user": admin.to_dict()}


@router.post("/auth/logout")
def logout(response: Response, admin: AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    revoke_account_sessions(db, admin)
    clear_session_cookie(response, ADMIN_SESSION_COOKIE)
    return {"message": "Logout berhasil"}


@router.get("/auth/me")
def me(admin: AdminUser = Depends(get_current_admin)):
    return admin.to_dict()


@router.put("/auth/name")
def update_own_name(
    payload: AdminNameUpdateRequest,
    admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    admin.full_name = payload.full_name
    db.commit()
    db.refresh(admin)
    return {"message": "Profil berhasil diperbarui", "user": admin.to_dict()}


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
    admin.full_name = payload.full_name
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
def forgot_password(payload: PasswordForgotRequest, request: Request, db: Session = Depends(get_db)):
    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Email atau username wajib diisi")
    auth_throttle.ensure_password_reset_request_allowed(request, "admin", identifier)

    admin = (
        db.query(AdminUser)
        .filter((AdminUser.email == identifier) | (AdminUser.username == identifier))
        .first()
    )
    if not admin or not admin.is_active or not admin.email:
        return {"message": RESET_REQUEST_MESSAGE}

    settings = get_settings()
    token = create_admin_password_reset_token(admin)
    reset_url = f"{settings.frontend_base_url.rstrip('/')}/reset-password#kind=admin&token={token}"
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

    ensure_current_session(reset_payload, admin)
    changed = db.query(AdminUser).filter(
        AdminUser.id == admin.id, AdminUser.password_hash == admin.password_hash
    ).update({AdminUser.password_hash: hash_password(payload.password)}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="Token reset password tidak valid")
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
    request: Request,
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

    if not isinstance(key_data, dict) or key_data.get("type") != "service_account":
        raise HTTPException(status_code=400, detail="File is not a GEE service-account key (type != service_account)")
    required = ("type", "project_id", "private_key", "client_email")
    missing = [f for f in required if f not in key_data]
    if missing:
        raise HTTPException(status_code=400, detail=f"Key file missing required fields: {missing}")
    if not all(isinstance(key_data[field], str) and key_data[field] for field in required):
        raise HTTPException(status_code=400, detail="Key metadata must be non-empty strings")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,127}", key_data["project_id"]) or len(key_data["client_email"]) > 256:
        raise HTTPException(status_code=400, detail="Invalid credential project or email metadata")

    project_id = key_data["project_id"]
    client_email = key_data["client_email"]
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    bucket_path = f"gee_{project_id}_{ts}.json"

    try:
        storage_service.upload_credential(bucket_path, raw)
    except storage_service.StorageNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    cred = GEECredential(
        label=label,
        project_id=project_id,
        client_email=client_email,
        json_filename=file.filename or bucket_path,
        bucket_path=bucket_path,
        is_active=False,
        uploaded_by=admin.id,
        notes=notes,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    audit_service.log_audit(db, admin.id, "credential.create", "gee_credential", cred.id, detail={"label": label, "project_id": project_id})

    if activate:
        from app.services import credential_activation
        try:
            credential_activation.activate(db, cred.id, admin.id)
            request.app.state.ee_initialized = True
        except credential_activation.CredentialActivationError as exc:
            if exc.runtime_initialized is not None:
                request.app.state.ee_initialized = exc.runtime_initialized
            return {"message": "Credential uploaded; activation failed", "credential": cred.to_dict(), "activation_error": exc.code}

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
def activate_gee_credential(cred_id: int, request: Request, admin: AdminUser = Depends(require_permission("credential.write")), db: Session = Depends(get_db)):
    cred = db.get(GEECredential, cred_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    from app.services import credential_activation
    try:
        result = credential_activation.activate(db, cred_id, admin.id)
    except credential_activation.CredentialActivationError as exc:
        if exc.runtime_initialized is not None:
            request.app.state.ee_initialized = exc.runtime_initialized
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from None
    request.app.state.ee_initialized = result["gee_initialized"]
    return {"message": "Credential activated", **result}


@router.post("/gee/credentials/{cred_id}/probe")
def probe_gee_credential(cred_id: int, admin: AdminUser = Depends(require_permission("credential.write")), db: Session = Depends(get_db)):
    from app.services import credential_activation
    if db.get(GEECredential, cred_id) is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return credential_activation.check(db, cred_id, admin.id)


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
    return configure_arcgis_from_db(db, force=True).get_status()


@router.put("/arcgis/config")
async def update_arcgis_config(
    request: Request,
    admin: AdminUser = Depends(require_permission("secret.write")),
    db: Session = Depends(get_db),
):
    """Save ArcGIS connection options and an optional write-only API key."""
    body = await read_bounded_json_object(request)
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

    client = configure_arcgis_from_db(db, force=True)
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
    body = await read_bounded_json_object(request)
    if isinstance(body, dict) and "updates" in body and isinstance(body["updates"], list):
        items = [(u["key"], u.get("value")) for u in body["updates"]]
    else:
        items = [(k, v) for k, v in body.items()]

    updated = []
    errors = []
    warnings: list[str] = []
    rejected: dict[str, str] = {}
    ai_updates = {k: v for k, v in items if isinstance(k, str) and k.startswith("ai.")}
    if ai_updates:
        from app.services import ai_provider_service

        rejected, warnings = await run_in_threadpool(
            ai_provider_service.validate_ai_config_update,
            ai_updates,
            lambda k: config_service.get_setting(db, k, "") or "",
        )
    pool_style_errors = config_service.validate_carbon_pool_visualization_updates(
        db, {key: value for key, value in items if isinstance(key, str)}
    )
    for key, value in items:
        if key in rejected:
            errors.append({"key": key, "error": rejected[key]})
            continue
        if key in pool_style_errors:
            errors.append({"key": key, "error": pool_style_errors[key]})
            continue
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
            configure_arcgis_from_db(db, force=True)
    return {"message": "Config updated", "updated": updated, "errors": errors, "warnings": warnings}


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
        configure_arcgis_from_db(db, force=True)
    return {"message": f"Reset {count} config value(s) to defaults"}


# -- Model manager --
def _compute_uploaded_model_evaluation(
    raw: bytes,
    model_path: Path,
    feature_names: list[str],
    target_column: str,
    group_column: str,
    training_groups: set[str],
    model_type: str,
) -> tuple[str, dict, dict]:
    try:
        text_data = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text_data, newline=""))
        headers = reader.fieldnames or []
    except (UnicodeDecodeError, csv.Error) as exc:
        raise HTTPException(400, "Evaluation file must be a valid UTF-8 CSV") from exc
    required = set(feature_names) | {target_column, group_column}
    if len(headers) != len(set(headers)) or set(headers) != required:
        raise HTTPException(422, detail={"code": "evaluation_schema_mismatch", "expected_columns": sorted(required)})
    if target_column in feature_names or group_column in feature_names or target_column == group_column:
        raise HTTPException(422, "Feature, target and independent-group columns must be distinct")

    rows: list[list[float]] = []
    targets: list[float] = []
    groups: set[str] = set()
    try:
        for row in reader:
            if (len(rows) + 1) * len(feature_names) > 1_000_000:
                raise HTTPException(413, "Evaluation matrix may contain at most 1,000,000 feature values")
            values = [float(row[name]) for name in feature_names]
            target = float(row[target_column])
            group = str(row[group_column] or "").strip()
            if not group or not all(math.isfinite(value) for value in [*values, target]):
                raise ValueError("empty group or non-finite numeric value")
            rows.append(values)
            targets.append(target)
            groups.add(group)
    except (TypeError, ValueError, KeyError, csv.Error) as exc:
        raise HTTPException(422, "Evaluation CSV contains missing or non-numeric values") from exc
    if len(rows) < 10 or len(groups) < 5:
        raise HTTPException(422, detail={"code": "insufficient_independent_samples", "minimum_rows": 10, "minimum_groups": 5})
    if len(training_groups) < 5:
        raise HTTPException(422, detail={"code": "insufficient_training_groups", "minimum_training_groups": 5})
    if len(groups & training_groups) > 0:
        raise HTTPException(422, detail={"code": "evaluation_leakage", "message": "Evaluation group IDs overlap the declared training groups"})

    raw_model = model_path.read_bytes()
    from app.inference.safe_model import OnnxRegressor
    import numpy as np

    model = OnnxRegressor(raw_model, {})
    observed = np.asarray(targets, dtype=float)
    predicted = model.predict(np.asarray(rows, dtype=float))
    if model_type == "landcover":
        classes = sorted(set(observed.tolist()))
        predicted_classes = np.rint(predicted)
        if not np.allclose(predicted, predicted_classes, atol=1e-5) or not set(predicted_classes.tolist()).issubset(set(classes)):
            raise ValueError("Land-cover ONNX output must be encoded class labels present in the evaluation data")
        recalls = []
        matrix = []
        for actual_class in classes:
            row_counts = [int(np.sum((observed == actual_class) & (predicted_classes == predicted_class))) for predicted_class in classes]
            matrix.append(row_counts)
            total = int(np.sum(observed == actual_class))
            recalls.append(row_counts[classes.index(actual_class)] / total if total else 0.0)
        metrics = {"metric_type": "classification", "sample_count": len(rows),
            "independent_group_count": len(groups), "class_labels": classes,
            "accuracy": float(np.mean(predicted_classes == observed)),
            "balanced_accuracy": float(np.mean(recalls)), "confusion_matrix": matrix,
            "status": "computed"}
    else:
        residual = predicted - observed
        mse = float(np.mean(residual ** 2))
        denominator = float(np.sum((observed - np.mean(observed)) ** 2))
        metrics = {"metric_type": "regression", "sample_count": len(rows),
            "independent_group_count": len(groups), "rmse": float(np.sqrt(mse)),
            "mae": float(np.mean(np.abs(residual))),
            "bias_prediction_minus_observed": float(np.mean(residual)),
            "r2": float(1 - np.sum(residual ** 2) / denominator) if denominator > 0 else None,
            "status": "computed"}
    return hashlib.sha256(raw_model).hexdigest(), metrics, {
        "feature_names": feature_names,
        "target_column": target_column,
        "group_column": group_column,
        "group_count": len(groups),
        "evaluation_group_ids_sha256": hashlib.sha256("\n".join(sorted(groups)).encode()).hexdigest(),
        "declared_training_group_count": len(training_groups),
        "declared_training_group_ids_sha256": hashlib.sha256("\n".join(sorted(training_groups)).encode()).hexdigest(),
        "provenance_status": "declared_by_evaluator_not_independently_verified",
    }


@router.get("/models")
def admin_models_list(
    request: Request,
    model_type: str | None = None,
    admin: AdminUser = Depends(require_permission("model.read")),
    db: Session = Depends(get_db),
):
    model_columns = {column["name"] for column in sa_inspect(db.get_bind()).get_columns("uploaded_models")}
    if "lifecycle_state" not in model_columns:
        # Legacy deployments can still list models, but cannot claim lifecycle
        # approval or safely execute review/archive actions.
        legacy_columns = (
            "id", "name", "display_name", "model_type", "algorithm", "filename",
            "file_size_kb", "is_default", "is_active", "is_legacy", "description",
            "version", "metrics", "feature_names", "metadata_json", "uploaded_by",
            "uploaded_at", "updated_at",
        )
        selected_columns = [name for name in legacy_columns if name in model_columns]
        if "id" not in selected_columns:
            raise HTTPException(status_code=503, detail="Model catalog schema is unavailable")
        legacy_table = Table("uploaded_models", MetaData(), autoload_with=db.get_bind())
        statement = select(*(legacy_table.c[name] for name in selected_columns))
        if model_type and "model_type" in model_columns:
            statement = statement.where(legacy_table.c.model_type == model_type)
        order_column = "updated_at" if "updated_at" in model_columns else "uploaded_at" if "uploaded_at" in model_columns else "id"
        raw_rows = db.execute(statement.order_by(legacy_table.c[order_column].desc())).mappings().all()

        def decode_json_value(value, default):
            for _ in range(2):
                if not isinstance(value, str):
                    break
                try:
                    value = json.loads(value)
                except (TypeError, json.JSONDecodeError):
                    return default
            return value if isinstance(value, type(default)) else default

        models = []
        for row in raw_rows:
            item = dict(row)
            item["display_name"] = item.get("display_name") or item.get("name")
            item["is_active"] = bool(item.get("is_active", False))
            item["is_default"] = bool(item.get("is_default", False))
            item["is_legacy"] = bool(item.get("is_legacy", False))
            item["metrics"] = decode_json_value(item.get("metrics"), {})
            item["feature_names"] = decode_json_value(item.get("feature_names"), [])
            item["metadata_json"] = decode_json_value(item.get("metadata_json"), {})
            for date_field in ("uploaded_at", "updated_at"):
                value = item.get(date_field)
                item[date_field] = value.isoformat() if hasattr(value, "isoformat") else value
            item["lifecycle_state"] = "legacy_unmigrated"
            item["approved_by"] = None
            item["approved_at"] = None
            models.append(item)

        uploader_ids = {item.get("uploaded_by") for item in models if item.get("uploaded_by") is not None}
        uploader_names = {}
        if uploader_ids:
            try:
                uploader_names = {
                    user_id: username
                    for user_id, username in db.query(AdminUser.id, AdminUser.username)
                    .filter(AdminUser.id.in_(uploader_ids))
                    .all()
                }
            except Exception:
                logger.warning("Could not resolve legacy model uploader names")
        for item in models:
            item["uploaded_by"] = uploader_names.get(item.get("uploaded_by"))

        if is_datatables_request(request):
            params = request.query_params
            search_value = (params.get("search[value]") or "").strip().casefold()
            filtered = models
            if search_value:
                searchable_fields = ("name", "display_name", "algorithm", "model_type", "description")
                filtered = [
                    item for item in models
                    if any(search_value in str(item.get(field) or "").casefold() for field in searchable_fields)
                ]
            try:
                draw = max(0, int(params.get("draw", "0") or 0))
                start = max(0, int(params.get("start", "0") or 0))
                length = int(params.get("length", "10") or 10)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid DataTables pagination parameters") from exc
            page = filtered[start:] if length == -1 else filtered[start : start + max(0, length)]
            return {
                "draw": draw,
                "recordsTotal": len(models),
                "recordsFiltered": len(filtered),
                "data": page,
            }
        return {"models": models}

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
    feature_names: str | None = Form(None),
    set_default: bool = Form(False),
    admin: AdminUser = Depends(require_permission("model.write")),
    db: Session = Depends(get_db),
):
    _ensure_model_execution_permission(admin)
    name, display_name = name.strip(), display_name.strip()
    if not name or not display_name or len(name) > 255 or len(display_name) > 255 or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise HTTPException(status_code=422, detail="Model slug and display name are required; slug may use letters, numbers, dot, underscore and hyphen")
    if model_type not in ("carbon", "vegetation", "landcover"):
        raise HTTPException(status_code=400, detail="model_type must be carbon, vegetation, or landcover")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_MODEL_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

    if db.query(UploadedModel).filter_by(name=name).first():
        raise HTTPException(status_code=400, detail=f"Model name {name!r} already exists")

    raw = await file.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Model file too large (max 128MB)")
    from app.inference.safe_model import validate_onnx
    try:
        await run_in_threadpool(validate_onnx, raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid or unsupported ONNX model") from exc

    settings = get_settings()
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dest = settings.model_path / f"{_safe_model_stem(name)}_{ts}{ext}"

    def _parse_json(s: str | None, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return default

    names = _parse_json(feature_names, [])
    if not isinstance(names, list) or len(names) > 512 or not all(isinstance(n, str) and 0 < len(n) <= 100 for n in names):
        raise HTTPException(status_code=400, detail="Invalid model feature names")
    import hashlib
    model_algorithm = algorithm or "linear"
    model_metadata = {"algorithm": model_algorithm, "feature_names": names,
                      "format": "onnx", "artifact_sha256": hashlib.sha256(raw).hexdigest(),
                      "provenance_status": "artifact_uploaded_by_admin_unverified"}
    if set_default:
        raise HTTPException(status_code=400, detail="Review and activate the uploaded model before setting a default")
    await run_in_threadpool(dest.write_bytes, raw)
    await run_in_threadpool(dest.with_suffix('.json').write_text, json.dumps(model_metadata), encoding="utf8")

    model = UploadedModel(
        name=name,
        display_name=display_name,
        model_type=model_type,
        algorithm=model_algorithm,
        filename=dest.name,
        filepath=str(dest.resolve()),
        file_size_kb=round(len(raw) / 1024, 2),
        is_default=False,
        is_active=False,
        lifecycle_state="experimental",
        is_legacy=False,
        description=description,
        version=version,
        metrics=None,
        feature_names=_parse_json(feature_names, []),
        metadata_json=model_metadata,
        artifact_sha256=model_metadata["artifact_sha256"],
        uploaded_by=admin.id,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    audit_service.log_audit(db, admin.id, "model.create", "uploaded_model", model.id, detail={"name": name, "model_type": model_type})
    return {"message": "Model uploaded", "model": model.to_dict(include_path=True)}


@router.post("/models/{model_id}/evaluations", status_code=201)
async def admin_model_evaluate(
    model_id: int,
    file: UploadFile = File(...),
    dataset_name: str = Form(..., min_length=1, max_length=255),
    target_column: str = Form(..., min_length=1, max_length=128),
    target_unit: str = Form(..., min_length=1, max_length=64),
    training_groups_json: str = Form(..., min_length=2, max_length=1_000_000),
    group_column: str = Form("group_id", min_length=1, max_length=128),
    split_method: str = Form(...),
    dataset_source: str | None = Form(None, max_length=1000),
    admin: AdminUser = Depends(require_permission("model.write")),
    db: Session = Depends(get_db),
):
    model = db.execute(select(UploadedModel).where(UploadedModel.id == model_id).with_for_update()
                       .execution_options(populate_existing=True)).scalar_one_or_none()
    if model is None:
        raise HTTPException(404, "Model not found")
    if (model.metadata_json or {}).get("format") != "onnx":
        raise HTTPException(409, "This model uses an internal evaluation pipeline and cannot be evaluated with the uploaded ONNX CSV endpoint")
    if model.lifecycle_state not in {"experimental", "evaluated"}:
        raise HTTPException(409, "This model version is no longer editable or evaluable")
    if split_method not in {"grouped_holdout", "spatial_holdout", "temporal_holdout", "independent_dataset"}:
        raise HTTPException(422, "Unsupported evaluation split method")
    feature_names = model.feature_names or []
    if not feature_names or len(feature_names) > 512 or len(set(feature_names)) != len(feature_names):
        raise HTTPException(409, "A unique feature_names schema is required before evaluation")
    try:
        training_groups_value = json.loads(training_groups_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "training_groups_json must be a JSON array of group IDs") from exc
    if not isinstance(training_groups_value, list) or not training_groups_value or len(training_groups_value) > 100_000:
        raise HTTPException(422, "At least one and at most 100,000 training group IDs are required")
    training_groups = {str(value).strip() for value in training_groups_value if str(value).strip()}
    if len(training_groups) != len(training_groups_value):
        raise HTTPException(422, "Training group IDs must be unique and non-empty")
    filename = (file.filename or "").lower()
    if not filename.endswith(".csv"):
        raise HTTPException(400, "Evaluation data must be uploaded as CSV")
    raw = await file.read(32 * 1024 * 1024 + 1)
    if not raw or len(raw) > 32 * 1024 * 1024:
        raise HTTPException(413, "Evaluation CSV must be between 1 byte and 32 MiB")
    from app.services.model_lifecycle import artifact_path
    exact_path = artifact_path(model)
    from app.inference.safe_model import validate_onnx
    try:
        await run_in_threadpool(validate_onnx, exact_path.read_bytes())
        dataset_sha, metrics_out, manifest = await run_in_threadpool(
            _compute_uploaded_model_evaluation, raw, exact_path, feature_names, target_column,
            group_column, training_groups, model.model_type,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - return a bounded inference/evaluation error
        raise HTTPException(422, "The ONNX model could not evaluate this dataset schema") from exc
    if dataset_sha != model.artifact_sha256:
        raise HTTPException(409, "Model artifact changed during evaluation")
    manifest.update({"split_method": split_method, "target_unit": target_unit.strip(),
                     "target_unit_status": "declared_by_evaluator_not_independently_verified"})
    if dataset_source:
        manifest["dataset_source"] = dataset_source.strip()
    evaluation = UploadedModelEvaluation(
        model_id=model.id,
        artifact_sha256=model.artifact_sha256,
        dataset_name=dataset_name.strip(),
        dataset_sha256=hashlib.sha256(raw).hexdigest(),
        dataset_source=dataset_source.strip() if dataset_source else None,
        manifest=manifest,
        metrics=metrics_out,
        evaluated_by=admin.id,
    )
    db.add(evaluation)
    db.flush()
    try:
        evaluation.dataset_storage_path = model_evaluation_storage.store(
            evaluation.id, evaluation.dataset_sha256, raw
        )
    except Exception as exc:  # noqa: BLE001 - do not accept a review dataset unless encrypted retention works
        db.rollback()
        raise HTTPException(503, "Encrypted storage for evaluation data is not configured") from exc
    model.lifecycle_state = "evaluated"
    model.approved_by = model.approved_at = model.approval_digest = None
    db.add(AuditLog(admin_user_id=admin.id, action="model.evaluate", resource_type="uploaded_model",
        resource_id=str(model.id), detail={"artifact_sha256": model.artifact_sha256,
            "dataset_sha256": hashlib.sha256(raw).hexdigest(), "metrics": metrics_out,
            "provenance_status": manifest["provenance_status"]}))
    db.commit()
    db.refresh(evaluation)
    return {"evaluation_id": evaluation.id, "artifact_sha256": evaluation.artifact_sha256,
            "dataset_sha256": evaluation.dataset_sha256, "manifest": evaluation.manifest,
            "metrics": evaluation.metrics, "dataset_retained_encrypted": True,
            "evaluated_at": evaluation.created_at.isoformat()}


@router.get("/models/{model_id}/lifecycle")
def admin_model_lifecycle(model_id: int, admin: AdminUser = Depends(require_permission("model.read")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    evaluations = db.query(UploadedModelEvaluation).filter_by(model_id=model.id).order_by(
        UploadedModelEvaluation.created_at.desc(), UploadedModelEvaluation.id.desc()).limit(20).all()
    from app.db.models.uploaded_model import UploadedModelReview
    reviews = db.query(UploadedModelReview).filter_by(model_id=model.id).order_by(
        UploadedModelReview.created_at.desc(), UploadedModelReview.id.desc()).limit(50).all()
    return {
        "model": model.to_dict(),
        "evaluations": [{"id": item.id, "artifact_sha256": item.artifact_sha256,
            "dataset_name": item.dataset_name, "dataset_sha256": item.dataset_sha256,
            "dataset_source": item.dataset_source, "manifest": item.manifest,
            "metrics": item.metrics, "evaluated_by": item.evaluated_by,
            "dataset_retained_encrypted": bool(item.dataset_storage_path),
            "created_at": item.created_at.isoformat()} for item in evaluations],
        "reviews": [{"id": item.id, "evaluation_id": item.evaluation_id,
            "artifact_sha256": item.artifact_sha256, "decision": item.decision,
            "reason": item.reason, "reviewer_id": item.reviewer_id,
            "created_at": item.created_at.isoformat()} for item in reviews],
    }


@router.post("/models/{model_id}/request-review")
def admin_model_request_review(model_id: int, admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    model_lifecycle.request_review(db, model, admin.id)
    return {"message": "Model submitted for independent review", "lifecycle_state": "review_pending"}


@router.post("/models/{model_id}/review")
def admin_model_review(model_id: int, payload: ModelReviewRequest,
                       admin: AdminUser = Depends(require_permission("model.approve")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    reviewed = model_lifecycle.review(db, model, admin.id, payload.decision, payload.reason)
    return {"message": f"Model review recorded: {reviewed.lifecycle_state}", "model": reviewed.to_dict()}


@router.post("/models/{model_id}/activate")
def admin_model_activate(model_id: int, admin: AdminUser = Depends(require_permission("model.approve")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if model is None:
        raise HTTPException(404, "Model not found")
    activated = model_lifecycle.activate(db, model, admin.id)
    return {"message": "Approved model activated", "model": activated.to_dict()}


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
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(model, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(model)
    audit_service.log_audit(db, admin.id, "model.metadata.update", "uploaded_model", model.id,
                            detail={"fields": sorted(changes)})
    return {"message": "Model updated", "model": model.to_dict(include_path=True)}


@router.delete("/models/{model_id}")
def admin_model_delete(model_id: int, admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    model_lifecycle.archive(db, model, admin.id)
    return {"message": "Model archived; artifact and review history retained"}


@router.post("/models/{model_id}/set-default")
def admin_model_set_default(model_id: int, admin: AdminUser = Depends(require_permission("model.approve")), db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    default_model = model_lifecycle.set_default(db, model, admin.id)
    return {"message": f"{default_model.name!r} set as default {default_model.model_type} model"}


@router.post("/models/import-legacy")
def admin_import_legacy_models(admin: AdminUser = Depends(require_permission("model.write")), db: Session = Depends(get_db)):
    _ensure_model_execution_permission(admin)
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
    role = db.query(Role).filter(Role.is_viewer.is_(True)).first()
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

    role = db.query(Role).filter(Role.is_viewer.is_(True)).with_for_update().first()
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
            searchable_columns=[AdminUser.username, AdminUser.full_name, AdminUser.email],
        )

    users = query.all()
    return {"users": [u.to_dict() for u in users]}


def _ensure_active_superuser_remains(db: Session, target: AdminUser) -> None:
    """Lock the active full-access set before removing one of its members."""
    if not target.is_superuser or not target.is_active:
        return
    active_superusers = (
        db.query(AdminUser.id)
        .filter(AdminUser.is_superuser.is_(True), AdminUser.is_active.is_(True))
        .order_by(AdminUser.id)
        .with_for_update()
        .all()
    )
    if len(active_superusers) <= 1:
        raise HTTPException(status_code=400, detail="At least one active full-access administrator must remain")


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
    if payload.is_superuser and not admin.is_superuser:
        raise HTTPException(403, "Only a superuser can grant full access")
    if payload.mfa_required and not admin.is_superuser:
        raise HTTPException(403, "Only a superuser can require MFA for an account")
    if payload.is_superuser and payload.role_id is not None:
        raise HTTPException(422, "Full access and an explicit role are mutually exclusive")
    _ensure_user_role_scope(admin, assigned_role)

    new_user = AdminUser(
        username=payload.username,
        full_name=payload.full_name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        is_active=payload.is_active,
        is_superuser=payload.is_superuser,
        mfa_required=payload.mfa_required,
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
    if not admin.is_superuser and target.is_superuser:
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat mengubah akun Full Access")
    if not admin.is_superuser and target.role is not None and _role_is_sensitive(target.role):
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat mengubah akun dengan akses credential atau secret")
    assigned_role = db.get(Role, payload.role_id) if payload.role_id is not None else None
    if payload.role_id is not None and assigned_role is None:
        raise HTTPException(status_code=400, detail="role_id does not exist")
    if "role_id" in payload.model_fields_set:
        if assigned_role is not None:
            _ensure_user_role_scope(admin, assigned_role)
        elif not admin.is_superuser:
            raise HTTPException(403, "Only a superuser can clear a role")
    if payload.is_superuser is not None:
        if not admin.is_superuser and payload.is_superuser != bool(target.is_superuser):
            raise HTTPException(403, "Only a superuser can change full access")
        if payload.is_superuser and payload.role_id is not None:
            raise HTTPException(422, "Full access and an explicit role are mutually exclusive")
        if target.id == admin.id and not payload.is_superuser:
            raise HTTPException(400, "Cannot revoke your own full access")
    removes_target_superuser = target.is_superuser and (
        payload.is_superuser is False
        or payload.is_active is False
        or ("role_id" in payload.model_fields_set and payload.role_id is not None)
    )
    if removes_target_superuser:
        if target.id == admin.id:
            raise HTTPException(status_code=400, detail="Cannot remove your own full-access role")
        _ensure_active_superuser_remains(db, target)
    if payload.is_active is False and target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    changes = {}
    if "mfa_required" in payload.model_fields_set and target.mfa_required != payload.mfa_required:
        if not admin.is_superuser:
            raise HTTPException(403, "Only a superuser can change an account's MFA requirement")
        require_recent_mfa(admin)
        target.mfa_required = payload.mfa_required
        target.session_version = (target.session_version or 0) + 1
        changes["mfa_required"] = payload.mfa_required
        changes["sessions"] = "revoked"
    if "full_name" in payload.model_fields_set:
        target.full_name = payload.full_name
        changes["full_name"] = payload.full_name
    if payload.email is not None:
        target.email = payload.email
        changes["email"] = payload.email
    if payload.is_active is not None:
        target.is_active = payload.is_active
        changes["is_active"] = payload.is_active
    if "role_id" in payload.model_fields_set:
        target.role_id = payload.role_id
        changes["role_id"] = payload.role_id
    if payload.is_superuser is not None:
        target.is_superuser = payload.is_superuser
        changes["is_superuser"] = payload.is_superuser
        if payload.is_superuser:
            target.role_id = None
    elif "role_id" in payload.model_fields_set and payload.role_id is not None:
        target.is_superuser = False
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
    if not admin.is_superuser and target.is_superuser:
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat menghapus akun Full Access")
    if not admin.is_superuser and target.role is not None and _role_is_sensitive(target.role):
        raise HTTPException(status_code=403, detail="Explicit Admin tidak dapat menghapus akun dengan akses credential atau secret")
    _ensure_active_superuser_remains(db, target)
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
_OVERPASS_IMPORT_DEADLINE_S = 240
_OVERPASS_MAX_CONCURRENT_REQUESTS = 4
_OVERPASS_RESPONSE_LIMIT_BYTES = 32 * 1024 * 1024
_overpass_semaphore = BoundedSemaphore(_OVERPASS_MAX_CONCURRENT_REQUESTS)

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
    # A key-regex selector matches any useful naming field in one predicate.
    # The previous four-way expansion multiplied every industry selector and
    # made nationwide Overpass requests much more expensive.
    name_selector = '[~"^(name|operator|brand|ref)$"~"."]'
    for itype in industry_types:
        for tag_selector in _OSM_TAGS_MAP.get(itype, []):
            for osm_type in ("way", "relation"):
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
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100_000),
    admin: AdminUser = Depends(require_permission("company.read")),
    db: Session = Depends(get_db),
):
    query = db.query(CompanyBoundary)
    if industry_type:
        query = query.filter_by(industry_type=industry_type)
    if province:
        query = query.filter_by(province=province)
    if search:
        query = query.filter(CompanyBoundary.name.icontains(search.strip(), autoescape=True))
    query = query.order_by(CompanyBoundary.name)

    if is_datatables_request(request):
        return datatables_response(
            request,
            query,
            row_mapper=lambda c: c.to_dict(),
            searchable_columns=[CompanyBoundary.name, CompanyBoundary.company_name, CompanyBoundary.province],
        )

    total = query.order_by(None).count()
    companies = query.offset(offset).limit(limit).all()
    return {
        "companies": [c.to_dict() for c in companies],
        "count": len(companies),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(companies) < total,
    }


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
        data = await read_bounded_json_object(request)
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
    data = await read_bounded_json_object(request)
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
    """Try Overpass mirrors in order; queries are split by industry before this call."""
    deadline = time.monotonic() + _OVERPASS_IMPORT_DEADLINE_S
    if not _overpass_semaphore.acquire(timeout=5):
        return None, "Overpass import capacity is busy; retry later"
    failures = []
    try:
        for server in _OVERPASS_SERVERS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append("Total Overpass import deadline exceeded")
                break
            try:
                resp = public_http.post(
                    server,
                    data={"data": overpass_query},
                    headers={"User-Agent": "SaveGeo/1.0 company-boundary importer"},
                    timeout=min(60, remaining),
                    max_bytes=_OVERPASS_RESPONSE_LIMIT_BYTES,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    if not isinstance(payload, dict):
                        failures.append("Overpass returned a non-object JSON response")
                        continue
                    if payload.get("remark") and not payload.get("elements"):
                        failures.append("Overpass query was rejected or timed out")
                        continue
                    return payload, ""
                failures.append(f"HTTP {resp.status_code} from Overpass")
            except (safe_requests.RequestException, ValueError, TypeError) as exc:
                failures.append(f"Overpass request failed: {type(exc).__name__}")
            logger.info("Overpass mirror failed; trying next configured mirror")
        return None, " | ".join(failures)
    finally:
        _overpass_semaphore.release()


# A single nationwide Overpass query per industry exceeds the public servers'
# runtime budget (HTTP 504 / "Query timed out"), so imports are split into
# island-group boxes (south, west, north, east). Boxes overlap slightly and
# results are de-duplicated by OSM id; the Indonesia area filter is still
# applied, so neighbouring countries inside a box are excluded.
_OSM_INDONESIA_REGIONS: list[tuple[str, tuple[float, float, float, float]]] = [
    ("Sumatera & Kepulauan Riau/Babel", (-6.2, 94.9, 6.2, 109.0)),
    ("Jawa & Bali", (-9.0, 105.0, -5.0, 115.8)),
    ("Kalimantan", (-4.3, 108.5, 7.2, 119.3)),
    ("Sulawesi", (-6.0, 118.7, 4.0, 125.5)),
    ("Nusa Tenggara", (-11.1, 115.8, -7.9, 125.3)),
    ("Maluku", (-8.4, 124.5, 3.0, 135.0)),
    ("Papua", (-9.2, 130.8, 1.1, 141.1)),
]
_OVERPASS_QUERY_TIMEOUT_S = 120
# A box that still times out is split into quadrants up to this depth
# (depth 2 = at most 16 sub-boxes per region).
_OSM_MAX_SPLIT_DEPTH = 2


def _osm_bbox_query(filters: list[str], bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    return (
        f"[out:json][timeout:{_OVERPASS_QUERY_TIMEOUT_S}][maxsize:536870912]"
        f"[bbox:{south:.4f},{west:.4f},{north:.4f},{east:.4f}];\n"
        'area["ISO3166-1"="ID"][admin_level=2]->.searchArea;\n'
        f"({chr(10).join(filters)});\n"
        "out geom qt;"
    )


def _split_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    south, west, north, east = bbox
    mid_lat, mid_lon = (south + north) / 2, (west + east) / 2
    return [
        (south, west, mid_lat, mid_lon),
        (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon),
        (mid_lat, mid_lon, north, east),
    ]


def _collect_osm_elements(industry_types: list[str], progress) -> tuple[list[dict], list[str], list[str]]:
    """Fetch named OSM area objects for each industry, region by region.

    Returns ``(elements, failed_types, warnings)``. An industry is reported in
    ``failed_types`` only when every region failed; partially failed regions
    become warnings so the rest of the data is still imported.
    """
    elements_by_id: dict[tuple, dict] = {}
    failed_types: list[str] = []
    warnings: list[str] = []
    for index, industry_type in enumerate(industry_types, start=1):
        filters = _osm_named_boundary_filters([industry_type])
        if not filters:
            continue
        succeeded = 0
        failed_regions: list[str] = []
        last_error = ""
        for region_name, region_bbox in _OSM_INDONESIA_REGIONS:
            pending = [(region_bbox, 0)]
            region_failed = False
            while pending:
                bbox, depth = pending.pop(0)
                progress(
                    f"Mengambil data OSM {industry_type} ({index}/{len(industry_types)}) - {region_name}"
                    + (f" bagian {depth}" if depth else "")
                    + "..."
                )
                osm_data, fetch_error = _fetch_osm_overpass(_osm_bbox_query(filters, bbox))
                if osm_data is None:
                    if depth < _OSM_MAX_SPLIT_DEPTH:
                        pending.extend((part, depth + 1) for part in _split_bbox(bbox))
                    else:
                        region_failed = True
                        last_error = fetch_error or "tidak ada respons dari mirror Overpass"
                    continue
                succeeded += 1
                if osm_data.get("remark"):
                    warnings.append(
                        f"{industry_type}/{region_name}: Overpass mengembalikan data parsial "
                        f"({str(osm_data['remark'])[:180]})"
                    )
                for element in osm_data.get("elements", []):
                    key = (element.get("type"), element.get("id"))
                    if key[0] and key[1] is not None:
                        elements_by_id.setdefault(key, element)
            if region_failed:
                failed_regions.append(region_name)
        if not succeeded:
            failed_types.append(f"{industry_type}: {last_error}")
        elif failed_regions:
            warnings.append(f"{industry_type}: sebagian wilayah gagal diambil ({', '.join(failed_regions)})")
    return list(elements_by_id.values()), failed_types, warnings


def _perform_osm_company_import(db: Session, admin: AdminUser, industry_types: list[str], progress) -> dict:
    selected_types = list(dict.fromkeys(itype for itype in industry_types if itype in _OSM_TAGS_MAP))
    if not selected_types:
        raise HTTPException(status_code=400, detail="Tidak ada tipe industri yang valid")

    elements, failed_types, warnings = _collect_osm_elements(selected_types, progress)

    if failed_types and len(failed_types) == len(selected_types):
        details = " ; ".join(failed_types)
        logger.warning("Overpass company import failed for all selected types: %s", details)
        raise RuntimeError(
            "Overpass gagal mengambil data OSM untuk semua jenis industri yang dipilih, "
            f"bahkan setelah query dipecah per wilayah. {details[:900]} Coba ulang beberapa saat lagi."
        )

    if failed_types:
        warnings.extend(f"Data {failure}" for failure in failed_types)

    progress(f"Menyimpan {len(elements)} objek OSM ke basis data...")
    imported, skipped, errors = 0, 0, []
    for element in elements:
        tags = element.get("tags", {})
        name = tags.get("name") or tags.get("operator") or tags.get("brand") or tags.get("ref")
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
                company_name=tags.get("operator") or tags.get("brand") or None,
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
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:10],
        "warnings": warnings[:10],
        "failed_types": [failure.split(":", 1)[0] for failure in failed_types],
    }


@router.post("/companies/import/osm", status_code=202)
async def admin_companies_import_osm(
    request: Request,
    admin: AdminUser = Depends(require_permission("company.write")),
):
    """Queue an OSM import so Overpass latency cannot hold open the gateway request."""
    try:
        data = await read_bounded_json_object(request)
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
    payload = await read_bounded_json_object(request)
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
    payload = await read_bounded_json_object(request)
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
def openrouter_models(
    free: str | None = None,
    q: str | None = None,
    _admin: AdminUser = Depends(get_current_admin_panel),
):
    import time as _time

    now = _time.time()
    if not _OR_MODEL_CACHE["data"] or now - _OR_MODEL_CACHE["ts"] > _OR_CACHE_TTL:
        try:
            r = public_http.get(
                "https://openrouter.ai/api/v1/models",
                timeout=10,
                max_bytes=8 * 1024 * 1024,
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


# -- AI provider health & Ollama model discovery --
@router.get("/ollama/models")
async def ollama_models(admin: AdminUser = Depends(require_permission("config.read")), db: Session = Depends(get_db)):
    """Models actually installed on the configured Ollama server (``/api/tags``)."""
    from app.services import ai_provider_service

    base_url = str(config_service.get_setting(db, "ai.ollama_base_url", "") or "")
    catalog = await run_in_threadpool(ai_provider_service.list_ollama_models, base_url)
    return {
        **catalog,
        "message": None if not catalog["error"] else ai_provider_service._MESSAGES.get(catalog["error"]),
    }


@router.get("/ai/health")
async def ai_provider_health(
    provider: str | None = None,
    model: str | None = None,
    force: bool = False,
    admin: AdminUser = Depends(require_permission("config.read")),
):
    """Active probe of the configured (or named) provider. Failures are cached ~30 s."""
    from app.agentic import agentic_ai
    from app.services import ai_provider_service

    cfg = await run_in_threadpool(agentic_ai.load_ai_config)
    chosen = (provider or cfg["provider"] or "").lower()
    chosen_model = model if model is not None else (cfg["model"] if chosen == cfg["provider"] else "")
    return await run_in_threadpool(
        lambda: ai_provider_service.check_provider_health(chosen, cfg, chosen_model or "", force=force)
    )

