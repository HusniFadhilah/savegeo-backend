"""Public user auth for the Disaster Intelligence Dashboard - a separate
`users` table + token kind (`typ: "user"`) from admin auth, so a user token
can never reach an admin route and vice versa (see
`app/core/security.py::get_current_admin`/`get_current_user`). `login()`
mirrors `app/api/routes/admin.py`'s `login()` handler exactly, just against
`User`/`create_user_access_token` instead of `AdminUser`/`create_access_token`.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    create_password_reset_token,
    create_user_access_token,
    decode_password_reset_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import PasswordForgotRequest, PasswordResetRequest, UserLoginRequest, UserRegisterRequest
from app.services.email_service import EmailDeliveryError, send_password_reset_email

router = APIRouter(prefix="/auth", tags=["user-auth"])

RESET_REQUEST_MESSAGE = (
    "Jika akun ditemukan, link reset password sudah dikirim ke email terdaftar."
)


@router.post("/register")
def register(payload: UserRegisterRequest, db: Session = Depends(get_db)):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.query(User).filter_by(username=payload.username).first():
        raise HTTPException(status_code=409, detail=f"Username {payload.username!r} already exists")
    if db.query(User).filter_by(email=payload.email).first():
        raise HTTPException(status_code=409, detail=f"Email {payload.email!r} already exists")

    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": create_user_access_token(user), "user": user.to_dict()}


@router.post("/login")
def login(payload: UserLoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=payload.username).first()
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    user.last_login = datetime.now(UTC)
    db.commit()
    return {"token": create_user_access_token(user), "user": user.to_dict()}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user.to_dict()


@router.post("/forgot-password")
def forgot_password(payload: PasswordForgotRequest, db: Session = Depends(get_db)):
    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Email atau username wajib diisi")

    user = (
        db.query(User)
        .filter((User.email == identifier) | (User.username == identifier))
        .first()
    )
    if not user or not user.is_active:
        return {"message": RESET_REQUEST_MESSAGE}

    settings = get_settings()
    token = create_password_reset_token(user)
    reset_url = f"{settings.frontend_base_url.rstrip('/')}/reset-password?token={token}"
    try:
        send_password_reset_email(user.email, user.username, reset_url)
    except EmailDeliveryError as exc:
        raise HTTPException(status_code=503, detail=f"Gagal mengirim email reset password: {exc}") from exc

    return {"message": RESET_REQUEST_MESSAGE}


@router.post("/reset-password")
def reset_password(payload: PasswordResetRequest, db: Session = Depends(get_db)):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password baru minimal 8 karakter")
    reset_payload = decode_password_reset_token(payload.token)
    user = db.get(User, int(reset_payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Token reset password tidak valid")

    user.password_hash = hash_password(payload.password)
    db.commit()
    return {"message": "Password berhasil diperbarui. Silakan login dengan password baru."}
