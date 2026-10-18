"""Regressions for the 18 October 2026 security audit (finding IDs F-02..F-11)."""
from __future__ import annotations

import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import actions, admin, analysis_jobs, arcgis, user_auth, utils, workflows
from app.core import auth_throttle, security
from app.core.security import create_access_token, create_user_access_token, get_current_app_viewer, hash_password
from app.db.models.admin_user import AdminUser
from app.db.models.analysis_provenance import AnalysisProvenance
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.system_config import SystemConfig
from app.db.models.user import User
from app.db.models.workflow import Workflow
from app.db.session import get_db


@pytest.fixture(autouse=True)
def _fresh_throttle():
    auth_throttle.reset_all()
    yield
    auth_throttle.reset_all()


@pytest.fixture()
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in (Role.__table__, Permission.__table__, role_permissions, User.__table__, AdminUser.__table__,
                  Workflow.__table__, SystemConfig.__table__):
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
    for router in (user_auth.router, admin.router, utils.router, arcgis.router, workflows.router):
        app.include_router(router, prefix="/api")

    def get_test_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_token(factory, user_id: int = 1) -> str:
    with factory() as db:
        return create_user_access_token(db.get(User, user_id))


# F-02, F-03, F-11: routes that spend server credentials or parse uploads need a session.
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/arcgis/tiles/ESRI_LULC/3/4/5"),
        ("get", "/api/utils/geocode?q=Jakarta"),
        ("get", "/api/utils/geocode/search?q=Jakarta"),
        ("post", "/api/utils/parse_aoi"),
        ("post", "/api/utils/convert_shp"),
        ("get", "/api/admin/openrouter/models"),
    ],
)
def test_proxy_and_upload_utilities_require_authentication(env, method, path):
    client, _factory = env
    response = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
    assert response.status_code == 401


def test_openrouter_model_list_rejects_public_user_tokens(env):
    client, factory = env
    response = client.get("/api/admin/openrouter/models", headers=_bearer(_user_token(factory)))
    assert response.status_code == 401


# F-03: a small archive must not expand into hundreds of megabytes in memory.
def test_convert_shp_rejects_decompression_bomb(env):
    client, factory = env
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open("bomb.shp", "w", force_zip64=True) as member:
            chunk = b"\0" * (1024 * 1024)
            for _ in range(utils._SHP_MAX_MEMBER_BYTES // len(chunk) + 1):
                member.write(chunk)
    assert len(buffer.getvalue()) < 5 * 1024 * 1024

    response = client.post(
        "/api/utils/convert_shp",
        json={"zip_b64": base64.b64encode(buffer.getvalue()).decode(), "name": "bomb.zip"},
        headers=_bearer(_user_token(factory)),
    )
    assert response.status_code == 413


# F-04: credential guessing is throttled per account without locking out other accounts.
@pytest.mark.parametrize(("prefix", "username", "other"), [("/api/auth", "alice", "bob"), ("/api/admin/auth", "admin", None)])
def test_login_failures_are_throttled_per_account(env, prefix, username, other):
    client, _factory = env
    for _ in range(10):
        bad = client.post(prefix + "/login", json={"username": username, "password": "wrong-password"})
        assert bad.status_code == 401
    blocked = client.post(prefix + "/login", json={"username": username, "password": "password-123"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    if other:
        assert client.post(prefix + "/login", json={"username": other, "password": "password-123"}).status_code == 200


def test_successful_login_clears_account_failures(env):
    client, _factory = env
    for _ in range(9):
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"})
    assert client.post("/api/auth/login", json={"username": "alice", "password": "password-123"}).status_code == 200
    for _ in range(9):
        assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401


@pytest.mark.parametrize("prefix", ["/api/auth", "/api/admin/auth"])
def test_password_reset_requests_are_throttled(env, prefix):
    client, _factory = env
    for _ in range(3):
        assert client.post(prefix + "/forgot-password", json={"identifier": "nobody"}).status_code == 200
    assert client.post(prefix + "/forgot-password", json={"identifier": "nobody"}).status_code == 429


# F-09: unknown usernames still pay for a password-hash verification.
def test_login_verification_runs_kdf_for_unknown_accounts(monkeypatch):
    calls = []
    monkeypatch.setattr(security, "verify_password", lambda plain, hashed: calls.append(hashed) or False)
    assert security.verify_login_password("guess", None) is False
    assert calls == [security._DUMMY_PASSWORD_HASH]


# F-06: unlisted workflows are reachable by link but never listed to other users.
def test_unlisted_workflows_are_not_listed_to_other_users(env):
    client, factory = env
    # Feature-neutral definition, so only visibility decides what is listed.
    definition = {"category": "general", "nodes": [{"id": "aoi", "type": "load_aoi"}]}
    with factory() as db:
        for name, visibility in (("shared-link", "unlisted"), ("published", "public"), ("own-private", "private")):
            db.add(Workflow(owner_id=1, name=name, visibility=visibility, workflow_json=definition, checksum="x"))
        db.commit()
    listed = client.get("/api/workflows", headers=_bearer(_user_token(factory, 2)))
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["published"]
    owner = client.get("/api/workflows", headers=_bearer(_user_token(factory, 1)))
    assert {item["name"] for item in owner.json()["items"]} == {"shared-link", "published", "own-private"}


# F-10: an explicit admin cannot take over an account holding credential/secret access.
def test_explicit_admin_cannot_modify_account_with_sensitive_role(env, monkeypatch):
    client, factory = env
    monkeypatch.setattr(admin.audit_service, "log_audit", lambda *args, **kwargs: None)
    with factory() as db:
        users_write = Permission(code="users.write", description="users")
        secret_write = Permission(code="secret.write", description="secret")
        operator = Role(name="operator", permissions=[users_write])
        keeper = Role(name="keeper", permissions=[secret_write])
        db.add_all([users_write, secret_write, operator, keeper])
        db.flush()
        actor = AdminUser(username="operator-1", password_hash=hash_password("password-123"), role_id=operator.id)
        target = AdminUser(username="keeper-1", password_hash=hash_password("password-123"), role_id=keeper.id)
        db.add_all([actor, target])
        db.commit()
        token = create_access_token(actor)
        target_id = target.id

    update = client.put(f"/api/admin/users/{target_id}", json={"new_password": "attacker-chosen-1"}, headers=_bearer(token))
    assert update.status_code == 403
    assert client.delete(f"/api/admin/users/{target_id}", headers=_bearer(token)).status_code == 403
    assert client.post("/api/admin/auth/login", json={"username": "keeper-1", "password": "password-123"}).status_code == 200


# F-05: durable action records are private to the account that submitted the job.
def test_action_records_are_private_to_the_job_owner(monkeypatch, tmp_path):
    action_id = "0b5f3c1e-6c1b-4c1a-9b9e-5a0f3d7c2a11"
    (tmp_path / f"{action_id}.json").write_text(json.dumps({"job_id": action_id, "owner": "user:1"}), encoding="utf-8")
    monkeypatch.setattr(analysis_jobs, "_jobs_dir", lambda: tmp_path)
    monkeypatch.setattr(actions, "ensure_job_feature_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(actions, "get_action", lambda _db, value: {"id": value})
    provenance = SimpleNamespace(analysis_type="carbon", parameters={}, created_by_user_id=1)

    class FakeSession:
        def get(self, model, key):
            return provenance if model is AnalysisProvenance and key == action_id else None

    current = {"viewer": User(id=1, username="alice")}
    app = FastAPI()
    app.include_router(actions.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: FakeSession()
    app.dependency_overrides[get_current_app_viewer] = lambda: current["viewer"]
    with TestClient(app) as client:
        assert client.get(f"/api/actions/{action_id}").status_code == 200
        for intruder in (User(id=2, username="bob"), AdminUser(id=1, username="admin")):
            current["viewer"] = intruder
            assert client.get(f"/api/actions/{action_id}").status_code == 404
            assert client.get(f"/api/actions/{action_id}/outputs").status_code == 404
