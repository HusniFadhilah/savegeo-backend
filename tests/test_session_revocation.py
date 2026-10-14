"""HTTP regressions for VAPT logout replay and password-reset token replay."""
from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import admin, chat, user_auth, workflows
from app.core.config import get_settings
from app.core.security import (
    create_access_token, create_user_access_token, create_password_reset_token,
    create_admin_password_reset_token, hash_password,
    get_current_app_viewer,
)
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.models.chat_session import ChatSession
from app.db.models.chat_message import ChatMessage
from app.db.models.role import Role, Permission, role_permissions
from app.db.session import get_db


@pytest.fixture()
def auth_app():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in (Role.__table__, Permission.__table__, role_permissions, User.__table__,
                  AdminUser.__table__, ChatSession.__table__, ChatMessage.__table__):
        table.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([
            User(id=1, username="alice", email="alice@example.test", password_hash=hash_password("password-123")),
            User(id=2, username="bob", email="bob@example.test", password_hash=hash_password("password-123")),
            AdminUser(id=1, username="admin", email="admin@example.test", password_hash=hash_password("password-123")),
        ])
        db.commit()

    app = FastAPI()
    for router in (user_auth.router, admin.router, chat.router):
        app.include_router(router, prefix="/api")

    @app.get("/api/qa/tile")
    def tile(viewer=Depends(get_current_app_viewer)):
        return {"id": viewer.id}

    @app.get("/api/qa/workflow")
    def workflow(viewer=Depends(workflows.optional_user)):
        return {"id": viewer.id if viewer else None}

    def get_test_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


@pytest.mark.parametrize("kind", ["user", "admin"])
def test_logout_revokes_cookie_bearer_and_query_replay_across_clients(auth_app, kind):
    client, factory = auth_app
    prefix = "/api/auth" if kind == "user" else "/api/admin/auth"
    username = "alice" if kind == "user" else "admin"
    cookie_name = f"savegeo_{kind}_session"
    response = client.post(prefix + "/login", json={"username": username, "password": "password-123"})
    assert response.status_code == 200
    stolen = client.cookies.get(cookie_name)
    with factory() as db:
        unrelated_token = create_user_access_token(db.get(User, 2))
    assert client.get(prefix + "/me").status_code == 200
    assert client.post(prefix + "/logout").status_code == 200
    assert client.cookies.get(cookie_name) is None
    assert client.get(prefix + "/me", headers={"Authorization": f"Bearer {stolen}"}).status_code == 401
    assert client.get("/api/qa/tile", params={"token": stolen}).status_code == 401
    if kind == "user":
        assert client.get("/api/qa/workflow", headers={"Authorization": f"Bearer {stolen}"}).json()["id"] is None
    assert client.get("/api/chat/sessions", headers={"Authorization": f"Bearer {stolen}"}).status_code == 401
    assert client.get("/api/chat/sessions", headers={"Authorization": f"Bearer {unrelated_token}"}).status_code == 200
    client.cookies.set(cookie_name, stolen)
    assert client.get(prefix + "/me").status_code == 401
    client.cookies.clear()
    assert client.post(prefix + "/login", json={"username": username, "password": "password-123"}).status_code == 200
    assert client.get(prefix + "/me").status_code == 200


@pytest.mark.parametrize("kind", ["user", "admin"])
def test_reset_password_revokes_access_and_reset_token_is_single_use(auth_app, kind):
    client, factory = auth_app
    model = User if kind == "user" else AdminUser
    access = create_user_access_token if kind == "user" else create_access_token
    reset = create_password_reset_token if kind == "user" else create_admin_password_reset_token
    prefix = "/api/auth" if kind == "user" else "/api/admin/auth"
    with factory() as db:
        account = db.get(model, 1)
        old_token = access(account)
        reset_token = reset(account)
    body = {"token": reset_token, "password": "new-password-456"}
    assert client.post(prefix + "/reset-password", json=body).status_code == 200
    assert client.get(prefix + "/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    assert client.post(prefix + "/reset-password", json=body).status_code == 401
    assert client.post(prefix + "/login", json={
        "username": "alice" if kind == "user" else "admin", "password": "new-password-456"
    }).status_code == 200


@pytest.mark.parametrize("sub", ["not-an-id", "0", "-1", 1])
def test_signed_malformed_subject_and_legacy_tokens_fail_closed(auth_app, sub):
    client, _factory = auth_app
    settings = get_settings()
    now = datetime.now(UTC)
    token = jwt.encode({"sub": sub, "typ": "user", "iat": now, "exp": now + timedelta(minutes=5)},
                       settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_user_token_cannot_authenticate_as_admin_with_same_numeric_id(auth_app):
    client, factory = auth_app
    with factory() as db:
        token = create_user_access_token(db.get(User, 1))
    assert client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("kind", ["user", "admin"])
def test_valid_session_tokens_are_not_accepted_in_urls(auth_app, kind):
    client, factory = auth_app
    with factory() as db:
        token = create_user_access_token(db.get(User, 1)) if kind == "user" else create_access_token(db.get(AdminUser, 1))
    assert client.get("/api/qa/tile", params={"token": token}).status_code == 401
    assert client.get("/api/qa/tile", headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.parametrize("kind", ["user", "admin"])
def test_reset_email_uses_fragment_not_server_query(auth_app, kind, monkeypatch):
    from urllib.parse import urlsplit, parse_qs

    client, _ = auth_app
    sent = []
    module = user_auth if kind == "user" else admin
    monkeypatch.setattr(module, "send_password_reset_email", lambda _email, _name, link: sent.append(link))
    prefix = "/api/auth" if kind == "user" else "/api/admin/auth"
    response = client.post(prefix + "/forgot-password", json={"identifier": "alice" if kind == "user" else "admin"})
    assert response.status_code == 200
    link = urlsplit(sent[0])
    assert "token" not in parse_qs(link.query)
    assert parse_qs(link.fragment)["token"]


def test_http_chat_history_and_mutations_reject_other_users_and_admins(auth_app):
    client, factory = auth_app
    with factory() as db:
        alice = create_user_access_token(db.get(User, 1))
        outsiders = [create_user_access_token(db.get(User, 2)), create_access_token(db.get(AdminUser, 1))]
    created = client.post("/api/chat/sessions", json={"title": "Private Alice"},
                          headers={"Authorization": f"Bearer {alice}"})
    assert created.status_code == 201
    path = f"/api/chat/sessions/{created.json()['id']}"
    for token in outsiders:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/chat/sessions", headers=headers).json()["sessions"] == []
        assert client.get(path, headers=headers).status_code == 404
        assert client.put(path, json={"title": "Hijacked"}, headers=headers).status_code == 404
        assert client.delete(path, headers=headers).status_code == 404
    assert client.get(path, headers={"Authorization": f"Bearer {alice}"}).json()["title"] == "Private Alice"


def test_session_version_migration_preserves_accounts():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0032_session_revocation.py"
    spec = importlib.util.spec_from_file_location("session_revocation_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in ("users", "admin_users"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, username VARCHAR(64))")
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES (1, 'existing')")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        for table in ("users", "admin_users"):
            assert connection.exec_driver_sql(f"SELECT username, session_version FROM {table}").one() == ("existing", 0)
        with Operations.context(context):
            migration.downgrade()
        for table in ("users", "admin_users"):
            assert connection.exec_driver_sql(f"SELECT username FROM {table}").scalar_one() == "existing"
    engine.dispose()
