from __future__ import annotations

import pytest

from app.core.security import hash_password
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
    token = resp.json()["token"]

    me = client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == username


def test_login_wrong_password(client, temp_admin):
    username, _ = temp_admin
    resp = client.post("/api/admin/auth/login", json={"username": username, "password": "wrong"})
    assert resp.status_code == 401
