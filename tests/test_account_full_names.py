from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import admin, user_auth
from app.core.security import get_current_admin, get_current_admin_panel, get_current_user
from app.db.base import Base
from app.db.models.admin_user import AdminUser
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.user import User
from app.db.session import get_db


@pytest.fixture()
def accounts(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Role.__table__, Permission.__table__, role_permissions, AdminUser.__table__, User.__table__])
    with Session(engine) as db:
        owner = AdminUser(username="owner", password_hash="unused", full_name="Nama Awal")
        target = AdminUser(username="viewer", password_hash="unused", full_name="Viewer Lama")
        ordinary = User(username="member", email="member@example.test", password_hash="unused")
        other = User(username="other", email="other@example.test", password_hash="unused", full_name="Tidak diubah")
        db.add_all([owner, target, ordinary, other])
        db.commit()
        app = FastAPI()
        app.include_router(admin.router, prefix="/api")
        app.include_router(user_auth.router, prefix="/api")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_admin] = lambda: owner
        app.dependency_overrides[get_current_admin_panel] = lambda: owner
        app.dependency_overrides[get_current_user] = lambda: ordinary
        monkeypatch.setattr(admin.audit_service, "log_audit", lambda *_args, **_kwargs: None)
        with TestClient(app) as client:
            yield client, db, owner, target, ordinary, other
    engine.dispose()


def test_user_can_edit_own_name_and_read_it_back_without_changing_login(accounts):
    client, db, _, _, member, other = accounts
    result = client.put("/api/auth/profile", json={"full_name": "  Siti Nur Aini  ", "id": other.id})
    assert result.status_code == 200
    assert result.json()["user"]["full_name"] == "Siti Nur Aini"
    db.expire_all()
    assert client.get("/api/auth/me").json()["full_name"] == "Siti Nur Aini"
    assert member.username == "member"
    assert other.full_name == "Tidak diubah"
    assert "password_hash" not in result.json()["user"]


def test_admin_can_set_and_clear_a_managed_users_full_name(accounts):
    client, db, _, target, _, _ = accounts
    result = client.put(f"/api/admin/users/{target.id}", json={"full_name": "Budi Santoso"})
    assert result.status_code == 200
    db.expire_all()
    assert target.full_name == "Budi Santoso"
    assert target.username == "viewer"
    client.put(f"/api/admin/users/{target.id}", json={"email": "budi@example.test"})
    assert target.full_name == "Budi Santoso"  # Older clients omit full_name.
    result = client.put(f"/api/admin/users/{target.id}", json={"full_name": "  "})
    assert result.json()["user"]["full_name"] is None


def test_admin_profile_and_viewer_self_service_include_full_name(accounts):
    client, _, owner, _, _, _ = accounts
    result = client.put("/api/admin/auth/profile", json={"username": "owner", "full_name": "Pemilik Akun"})
    assert result.status_code == 200
    assert client.get("/api/admin/auth/me").json()["full_name"] == "Pemilik Akun"
    result = client.put("/api/admin/auth/name", json={"full_name": "Nama Tampilan", "username": "ignored"})
    assert result.status_code == 200
    assert owner.username == "owner"
    assert owner.full_name == "Nama Tampilan"


@pytest.mark.parametrize("path", ["/api/auth/profile", "/api/admin/auth/name"])
def test_names_over_160_characters_rejected(accounts, path):
    client, *_ = accounts
    assert client.put(path, json={"full_name": "x" * 161}).status_code == 422


@pytest.mark.parametrize("path,dependency", [("/api/auth/profile", get_current_user), ("/api/admin/auth/name", get_current_admin)])
def test_profile_name_update_requires_authentication(accounts, path, dependency):
    client, *_ = accounts
    client.app.dependency_overrides.pop(dependency)
    assert client.put(path, json={"full_name": "Anonymous"}).status_code == 401


def test_full_name_migration_preserves_existing_accounts():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for name in ["users", "admin_users"]:
        Table(name, metadata, Column("id", Integer, primary_key=True), Column("username", String(64)))
    metadata.create_all(engine)
    spec = spec_from_file_location("full_name_migration", Path(__file__).parents[1]/"alembic/versions/0035_account_full_names.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        for name in ["users", "admin_users"]:
            connection.execute(text(f"INSERT INTO {name} (id, username) VALUES (1, 'existing')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        for name in ["users", "admin_users"]:
            assert connection.execute(text(f"SELECT username, full_name FROM {name}")).one() == ("existing", None)
    engine.dispose()
