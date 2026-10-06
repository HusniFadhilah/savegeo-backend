from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.api.routes import chat
from app.api.routes.agent import router as agent_router
from app.core.chat_ownership import chat_session_owner_key
from app.core.security import create_user_access_token, get_current_app_viewer
from app.core.viewer_features import DEFAULT_VIEWER_FEATURE_CODES
from app.db.models.admin_user import AdminUser
from app.db.models.chat_message import ChatMessage
from app.db.models.chat_session import ChatSession
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.user import User
from app.db.session import get_db
from app.services import agent_service


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        yield db
    engine.dispose()


def _user(user_id: int) -> User:
    return User(id=user_id, username=f"qa-{user_id}", email=f"qa-{user_id}@example.test", password_hash="unused")


def _request() -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/agent/control",
        "query_string": b"",
        "headers": [],
        "client": ("192.0.2.10", 4567),
        "server": ("savegeo.test", 443),
    })


def test_session_ids_are_namespaced_by_principal_type():
    assert chat_session_owner_key(_user(7)) == "user:7"
    admin = AdminUser(id=7, username="qa-admin", password_hash="unused")
    assert chat_session_owner_key(admin) == "admin:7"


@pytest.mark.parametrize(
    "route_path",
    ["/chat/sessions", "/chat/sessions/{session_id}"],
)
def test_chat_routes_require_an_authenticated_viewer(route_path: str):
    routes = [route for route in chat.router.routes if isinstance(route, APIRoute) and route.path == route_path]
    assert routes
    assert all(
        get_current_app_viewer in [dependency.call for dependency in route.dependant.dependencies]
        for route in routes
    )


def test_list_and_read_sessions_are_scoped_to_owner(db_session: Session):
    first = ChatSession(title="Milik user 1", owner_key=chat_session_owner_key(_user(1)))
    second = ChatSession(title="Milik user 2", owner_key=chat_session_owner_key(_user(2)))
    legacy = ChatSession(title="Riwayat lama", owner_key=None)
    db_session.add_all([first, second, legacy])
    db_session.commit()

    listed = chat.list_sessions(viewer=_user(1), db=db_session)
    assert [item["title"] for item in listed["sessions"]] == ["Milik user 1"]
    assert chat.get_session(first.id, viewer=_user(1), db=db_session)["title"] == "Milik user 1"

    with pytest.raises(HTTPException) as error:
        chat.get_session(second.id, viewer=_user(1), db=db_session)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as legacy_error:
        chat.get_session(legacy.id, viewer=_user(1), db=db_session)
    assert legacy_error.value.status_code == 404


def test_foreign_session_cannot_be_renamed_or_deleted(db_session: Session):
    foreign = ChatSession(title="Private title", owner_key=chat_session_owner_key(_user(2)))
    db_session.add(foreign)
    db_session.commit()
    session_id = foreign.id

    with pytest.raises(HTTPException) as rename_error:
        chat.update_session(session_id, chat.SessionUpdateRequest(title="Hijacked"), viewer=_user(1), db=db_session)
    assert rename_error.value.status_code == 404
    with pytest.raises(HTTPException) as delete_error:
        chat.delete_session(session_id, viewer=_user(1), db=db_session)
    assert delete_error.value.status_code == 404
    assert db_session.get(ChatSession, session_id).title == "Private title"


def test_new_sessions_record_authenticated_owner(db_session: Session):
    result = chat.create_session(
        chat.SessionCreateRequest(title="  Test session  "),
        _request(),
        viewer=_user(3),
        db=db_session,
    )
    saved = db_session.get(ChatSession, result["id"])
    assert result["title"] == "Test session"
    assert saved is not None
    assert saved.owner_key == "user:3"


def test_chat_http_routes_require_auth_and_bind_sessions_to_authenticated_user():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    User.__table__.create(engine)
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="chat-http-qa",
            email="chat-http-qa@example.test",
            password_hash="unused",
            is_active=True,
        )
        db.add(user)
        db.commit()
        token = create_user_access_token(user)

    app = FastAPI()
    app.include_router(chat.router, prefix="/api")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        assert client.get("/api/chat/sessions").status_code == 401
        created = client.post("/api/chat/sessions", json={"title": "Live route test"}, headers=headers)
        assert created.status_code == 201
        assert created.json()["title"] == "Live route test"
        listed = client.get("/api/chat/sessions", headers=headers)
        assert [item["id"] for item in listed.json()["sessions"]] == [created.json()["id"]]

    engine.dispose()


def test_agent_http_route_requires_auth_and_passes_owner_scope(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Role.__table__.create(engine)
    Permission.__table__.create(engine)
    role_permissions.create(engine)
    User.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        viewer_role = Role(
            name="viewer",
            feature_permissions_configured=True,
            permissions=[Permission(code=code) for code in DEFAULT_VIEWER_FEATURE_CODES],
        )
        user = User(
            username="agent-http-qa",
            email="agent-http-qa@example.test",
            password_hash="unused",
            is_active=True,
        )
        db.add_all([viewer_role, user])
        db.commit()
        token = create_user_access_token(user)
        expected_owner_key = f"user:{user.id}"

    app = FastAPI()
    app.include_router(agent_router, prefix="/api")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    captured: dict = {}

    def fake_run_agent_control(db, request, payload, owner_key, allowed_features=None):
        captured["owner_key"] = owner_key
        captured["allowed_features"] = allowed_features
        captured["payload"] = payload
        return {"intent": "help", "message": "ok", "actions": []}, 200, {}

    monkeypatch.setattr(agent_service, "run_agent_control", fake_run_agent_control)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        assert client.post("/api/agent/control", json={"message": "test"}).status_code == 401
        response = client.post("/api/agent/control", json={"message": "test"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["message"] == "ok"
    assert captured["owner_key"] == expected_owner_key
    assert captured["allowed_features"] == DEFAULT_VIEWER_FEATURE_CODES
    assert captured["payload"] == {"message": "test"}
    engine.dispose()


def test_owner_migration_preserves_legacy_rows_and_is_reversible():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0028_chat_session_ownership.py"
    )
    spec = importlib.util.spec_from_file_location("chat_session_owner_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, title VARCHAR(120) NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO chat_sessions (id, title) VALUES (1, 'Legacy')")
        operations = Operations(MigrationContext.configure(connection))
        with Operations.context(operations.migration_context):
            migration.upgrade()

        columns = {column["name"]: column for column in inspect(connection).get_columns("chat_sessions")}
        indexes = {index["name"] for index in inspect(connection).get_indexes("chat_sessions")}
        legacy = connection.exec_driver_sql(
            "SELECT id, title, owner_key FROM chat_sessions WHERE id = 1"
        ).mappings().one()
        assert columns["owner_key"]["nullable"] is True
        assert "ix_chat_sessions_owner_key" in indexes
        assert dict(legacy) == {"id": 1, "title": "Legacy", "owner_key": None}

        with Operations.context(operations.migration_context):
            migration.downgrade()
        assert "owner_key" not in {
            column["name"] for column in inspect(connection).get_columns("chat_sessions")
        }

    engine.dispose()


def test_agent_refuses_foreign_session_before_calling_model(db_session: Session, monkeypatch):
    foreign = ChatSession(title="Private", owner_key=chat_session_owner_key(_user(2)))
    db_session.add(foreign)
    db_session.commit()
    monkeypatch.setattr(agent_service.rate_limiter, "check", lambda _ip: (True, "", 0))
    monkeypatch.setattr(agent_service.rate_limiter, "get_limits", lambda: {"max_chars": 4000, "max_file_mb": 5})
    monkeypatch.setattr(agent_service, "geoai_with_ai", lambda *_args, **_kwargs: pytest.fail("model must not run"))

    body, status, _headers = agent_service.run_agent_control(
        db_session,
        _request(),
        {"message": "lanjutkan", "mode": "geoai", "session_id": foreign.id},
        owner_key=chat_session_owner_key(_user(1)),
    )

    assert status == 404
    assert body == {"error": "Chat session not found"}


def test_agent_keeps_direct_reference_context_for_carbon_viewer(db_session: Session, monkeypatch):
    direct_rows = [
        {
            "analysis_kind": "carbon",
            "dataset_name": "GEDI L4A",
            "effective_year": 2023,
            "tile_url": "https://tiles.example.test/layer?token=private",
        },
        {"analysis_kind": "landcover", "dataset_name": "Dynamic World"},
    ]
    captured: dict = {}
    monkeypatch.setattr(agent_service.rate_limiter, "check", lambda _ip: (True, "", 0))
    monkeypatch.setattr(agent_service.rate_limiter, "get_limits", lambda: {"max_chars": 4000, "max_file_mb": 5})

    def fake_geoai(_message, context, _db, _history, **_kwargs):
        captured["context"] = context
        return {"message": "ok", "actions": [], "warnings": [], "cards": []}

    monkeypatch.setattr(agent_service, "geoai_with_ai", fake_geoai)
    result, status, _headers = agent_service.run_agent_control(
        db_session,
        _request(),
        {
            "message": "berapa stok referensi?",
            "mode": "geoai",
            "context": {"results": {"direct": direct_rows, "vegetation": {"mean": 0.7}}},
        },
        owner_key="user:5",
        allowed_features={"app.feature.carbon_stock"},
    )

    assert status == 200
    assert result["message"] == "ok"
    assert captured["context"]["results"] == {
        "direct": [{key: value for key, value in direct_rows[0].items() if key != "tile_url"}],
    }
