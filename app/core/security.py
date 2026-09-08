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
from fastapi import Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db

_bearer_scheme = HTTPBearer(auto_error=False)

ADMIN_SESSION_COOKIE = "savegeo_admin_session"
USER_SESSION_COOKIE = "savegeo_user_session"

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
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(admin.id),
        "username": admin.username,
        "typ": "admin",
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_user_access_token(user: User) -> str:
    """Same shape as `create_access_token`, but for public app-user accounts
    (Disaster Intelligence Dashboard). `typ: "user"` keeps the two token kinds
    from being interchangeable - see `get_current_admin`/`get_current_user`."""
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "typ": "user",
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_password_reset_token(user: User) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "typ": "user_password_reset",
        "purpose": "password_reset",
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.password_reset_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_admin_password_reset_token(admin: AdminUser) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(admin.id),
        "username": admin.username,
        "typ": "admin_password_reset",
        "purpose": "password_reset",
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.password_reset_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_password_reset_token(token: str) -> dict:
    payload = decode_access_token(token)
    if payload.get("typ") != "user_password_reset" or payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password reset token")
    return payload


def decode_admin_password_reset_token(token: str) -> dict:
    payload = decode_access_token(token)
    if payload.get("typ") != "admin_password_reset" or payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password reset token")
    return payload


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc


def set_session_cookie(response: Response, name: str, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=name,
        value=token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.auth_cookie_samesite.lower(),
        path="/",
    )


def clear_session_cookie(response: Response, name: str) -> None:
    response.delete_cookie(key=name, path="/")


def _credential_or_cookie(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    *cookie_names: str,
) -> str | None:
    if credentials is not None:
        return credentials.credentials
    for cookie_name in cookie_names:
        value = request.cookies.get(cookie_name)
        if value:
            return value
    return None


def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    raw_token = _credential_or_cookie(request, credentials, ADMIN_SESSION_COOKIE)
    if raw_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_access_token(raw_token)
    # Tokens minted before the "typ" claim existed have no "typ" key - treated
    # as admin (legacy). A token explicitly minted as "user" is rejected here
    # so a User account can never reach an admin route with its own token.
    if payload.get("typ") not in (None, "admin"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    admin = db.get(AdminUser, int(payload["sub"]))
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin account not found or inactive")
    return admin


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Auth gate for the public Disaster Intelligence Dashboard. Mirrors
    `get_current_admin` exactly but resolves against `users`, and only ever
    accepts a token explicitly minted with `typ: "user"` - an admin token
    (typ "admin" or legacy typ-less) is rejected, not silently upgraded."""
    raw_token = _credential_or_cookie(request, credentials, USER_SESSION_COOKIE)
    if raw_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_access_token(raw_token)
    if payload.get("typ") != "user":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found or inactive")
    return user


def get_current_disaster_viewer(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    token: str | None = Query(default=None, description="JWT untuk permintaan tile Leaflet"),
    db: Session = Depends(get_db),
) -> User | AdminUser:
    """Auth gate for read-only Disaster Mapping views.

    The public disaster dashboard is still user-authenticated by default, but
    active admins should be able to inspect the same published disaster pages
    without creating a duplicate user account. This intentionally does not make
    user tokens valid for admin routes; it is scoped only to disaster viewer
    endpoints that explicitly depend on this function.
    """
    raw_token = _credential_or_cookie(request, credentials, USER_SESSION_COOKIE, ADMIN_SESSION_COOKIE) or token
    if raw_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_access_token(raw_token)
    token_type = payload.get("typ")

    if token_type == "user":
        user = db.get(User, int(payload["sub"]))
        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found or inactive")
        return user

    if token_type in (None, "admin"):
        admin = db.get(AdminUser, int(payload["sub"]))
        if admin is None or not admin.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin account not found or inactive")
        return admin

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")


def get_current_app_viewer(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    token: str | None = Query(default=None, description="JWT untuk permintaan tile atau client geospasial"),
    db: Session = Depends(get_db),
) -> User | AdminUser:
    """Authentication dependency for the protected SaveGeo application.

    Public users and active admins may use the analysis workspace, while the
    admin-only routes continue to depend on ``get_current_admin``. Keeping
    this as a named dependency makes the access policy explicit instead of
    relying only on the SPA route guard.
    """
    return get_current_disaster_viewer(request=request, credentials=credentials, token=token, db=db)


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
