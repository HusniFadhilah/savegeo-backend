from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.security import admin_has_permission, ensure_admin_panel_access, hash_password
from app.db.models.admin_user import AdminUser
from app.db.session import SessionLocal


@pytest.fixture()
def temp_admin(db_available):
    if not db_available:
        pytest.skip("DATABASE_URL not reachable in this environment")
    db = SessionLocal()
    username = "pytest_admin"
    try:
        existing = db.query(AdminUser).filter_by(username=username).first()
        if existing:
            db.delete(existing)
            db.commit()
        admin = AdminUser(username=username, password_hash=hash_password("pytest_password_123"), is_active=True)
        db.add(admin)
        db.commit()
        yield username, "pytest_password_123"
    finally:
        db.query(AdminUser).filter_by(username=username).delete()
        db.commit()
        db.close()


def test_login_and_me(client, temp_admin):
    username, password = temp_admin
    resp = client.post("/api/admin/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    assert "token" not in resp.json()
    set_cookie = resp.headers.get("set-cookie", "")
    assert "savegeo_admin_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie

    me = client.get("/api/admin/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == username


def test_login_wrong_password(client, temp_admin):
    username, _ = temp_admin
    resp = client.post("/api/admin/auth/login", json={"username": username, "password": "wrong"})
    assert resp.status_code == 401


def test_viewer_is_denied_admin_panel_access():
    viewer = SimpleNamespace(role=SimpleNamespace(name=" viewer "))

    with pytest.raises(HTTPException) as exc_info:
        ensure_admin_panel_access(viewer)

    assert exc_info.value.status_code == 403


def test_full_access_and_explicit_roles_are_distinguished():
    explicit_admin = SimpleNamespace(
        role_id=1,
        role=SimpleNamespace(permissions=[SimpleNamespace(code="config.read")]),
    )
    full_access = SimpleNamespace(role_id=None, role=None)

    assert admin_has_permission(full_access, "secret.read") is True
    assert admin_has_permission(explicit_admin, "config.read") is True
    assert admin_has_permission(explicit_admin, "secret.read") is False
