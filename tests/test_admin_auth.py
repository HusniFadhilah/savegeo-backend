from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect

from app.api.routes.admin import _ensure_active_superuser_remains
from app.core.security import admin_has_permission, ensure_admin_panel_access, hash_password
from app.db.models.admin_user import AdminUser
from app.db.session import SessionLocal


@pytest.fixture()
def temp_admin(db_available):
    if not db_available:
        pytest.skip("DATABASE_URL not reachable in this environment")
    db = SessionLocal()
    username = f"pytest_admin_{uuid4().hex}"
    admin_id = None
    try:
        present_columns = {column["name"] for column in inspect(db.bind).get_columns("admin_users")}
        required_columns = {column.name for column in AdminUser.__table__.columns}
        if not required_columns.issubset(present_columns):
            pytest.skip("Database schema is behind the current application migrations")
        admin = AdminUser(is_superuser=True, username=username, password_hash=hash_password("pytest_password_123"), is_active=True)
        db.add(admin)
        db.commit()
        admin_id = admin.id
        yield username, "pytest_password_123"
    finally:
        if admin_id is not None:
            db.query(AdminUser).filter_by(id=admin_id).delete()
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


def test_admin_can_update_own_profile(client, temp_admin):
    username, password = temp_admin
    login = client.post("/api/admin/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200

    updated_username = f"{username}_updated"
    updated_email = f"{username}@example.test"
    response = client.put(
        "/api/admin/auth/profile",
        json={"username": updated_username, "email": updated_email, "full_name": "Nama Admin Uji"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["username"] == updated_username
    assert response.json()["user"]["email"] == updated_email
    assert response.json()["user"]["full_name"] == "Nama Admin Uji"

    me = client.get("/api/admin/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == updated_username


def test_viewer_is_denied_admin_panel_access():
    viewer = SimpleNamespace(role=SimpleNamespace(is_viewer=True))

    with pytest.raises(HTTPException) as exc_info:
        ensure_admin_panel_access(viewer)

    assert exc_info.value.status_code == 403


def test_full_access_and_explicit_roles_are_distinguished():
    explicit_admin = SimpleNamespace(
        is_superuser=False,
        role_id=1,
        role=SimpleNamespace(permissions=[SimpleNamespace(code="config.read")]),
    )
    full_access = SimpleNamespace(is_superuser=True, role_id=None, role=None)
    unassigned = SimpleNamespace(is_superuser=False, role_id=None, role=None)

    assert admin_has_permission(full_access, "secret.read") is True
    assert admin_has_permission(unassigned, "secret.read") is False
    assert admin_has_permission(explicit_admin, "config.read") is True
    assert admin_has_permission(explicit_admin, "secret.read") is False


@pytest.mark.parametrize("remaining", [0, 1])
def test_admin_change_cannot_remove_the_last_active_superuser(remaining):
    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def with_for_update(self):
            return self

        def all(self):
            return [(1,)] * (remaining + 1)

    class Database:
        def query(self, _model):
            return Query()

    target = SimpleNamespace(is_superuser=True, is_active=True)
    if remaining:
        _ensure_active_superuser_remains(Database(), target)
    else:
        with pytest.raises(HTTPException) as exc_info:
            _ensure_active_superuser_remains(Database(), target)
        assert exc_info.value.status_code == 400
