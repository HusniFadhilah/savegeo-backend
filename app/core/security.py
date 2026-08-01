"""JWT auth + password hashing for the admin panel.

Ports `admin_routes.py`'s `require_admin` decorator / `_generate_token` helper
(legacy backend) into FastAPI dependency form. Token shape is kept identical
(claims: sub, username, exp, iat) so any client already holding a legacy token
format understanding is unaffected — only the signing secret changes source
(env var instead of Flask app.config, same semantics).
"""
from __future__ import annotations

import datetime as dt

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.admin_user import AdminUser
from app.db.session import get_db

_bearer_scheme = HTTPBearer(auto_error=False)

# Hash directly with `bcrypt` rather than via passlib's CryptContext: passlib is
# unmaintained and its bcrypt backend self-test crashes against bcrypt>=4.1
# (`ValueError: password cannot be longer than 72 bytes` on its own internal
# wrap-bug probe, plus `module 'bcrypt' has no attribute '__about__'` on the
# version-sniffing path) - a real incompatibility discovered when smoke-testing
# this backend, not a hypothetical. bcrypt's own API has no such issue.
_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    raw = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(admin: AdminUser) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(admin.id),
        "username": admin.username,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc


def get_current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_access_token(credentials.credentials)
    admin = db.get(AdminUser, int(payload["sub"]))
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin account not found or inactive")
    return admin


def require_permission(code: str):
    """Additive permission gate, layered on top of `get_current_admin`.

    An admin with `role_id IS NULL` (every admin created before this RBAC
    layer existed, and any admin created without an explicit role since) is
    treated as legacy full-access and always passes - this column was added
    nullable specifically so introducing roles/permissions cannot lock out
    an existing deployment's admin account. Only admins explicitly assigned a
    role are checked against that role's permission list.
    """

    def _dependency(admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
        if admin.role_id is None:
            return admin
        codes = {p.code for p in (admin.role.permissions if admin.role else [])}
        if code not in codes:
            raise HTTPException(status_code=403, detail=f"Missing permission: {code}")
        return admin

    return _dependency
