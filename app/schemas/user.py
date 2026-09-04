"""Pydantic request bodies for the public user auth routes
(`app/api/routes/user_auth.py`). Mirrors `app/schemas/admin.py`'s
`LoginRequest` style - plain, no extra validation beyond typing (password
min-length is checked in the route, same convention as `admin.py`'s
`admin_user_create`).
"""
from __future__ import annotations

from pydantic import BaseModel


class UserRegisterRequest(BaseModel):
    username: str
    email: str
    password: str


class UserLoginRequest(BaseModel):
    username: str
    password: str


class PasswordForgotRequest(BaseModel):
    identifier: str


class PasswordResetRequest(BaseModel):
    token: str
    password: str
