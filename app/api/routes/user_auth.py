"""Public user auth for the Disaster Intelligence Dashboard - a separate
`users` table + token kind (`typ: "user"`) from admin auth, so a user token
can never reach an admin route and vice versa (see
`app/core/security.py::get_current_admin`/`get_current_user`). `login()`
mirrors `app/api/routes/admin.py`'s `login()` handler exactly, just against
`User`/`create_user_access_token` instead of `AdminUser`/`create_access_token`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import create_user_access_token, get_current_user, hash_password, verify_password
from app.db.models.user import User
from app.db.session import get_db
from app.schemas.user import UserLoginRequest, UserRegisterRequest

router = APIRouter(prefix="/auth", tags=["user-auth"])


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
    user.last_login = datetime.now(timezone.utc)
    db.commit()
    return {"token": create_user_access_token(user), "user": user.to_dict()}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user.to_dict()
