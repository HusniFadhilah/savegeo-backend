import os

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import dmrv as dmrv_routes
from app.core.security import get_current_app_viewer
from app.db.base import Base
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_audit import DmrvAuditEvent, DmrvIdempotencyKey
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.user import User
from app.db.session import get_db
from app.services.dmrv.errors import register_dmrv_error_handler


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


TABLES = [
    User.__table__, AdminUser.__table__, Role.__table__, Permission.__table__, role_permissions,
    DmrvProject.__table__, ProjectMember.__table__, BoundaryVersion.__table__,
    DmrvAuditEvent.__table__, DmrvIdempotencyKey.__table__,
]
PROJECT = {"name": "Pilot ARR", "organization": "UNDIP", "methodology_id": "VM0047", "methodology_version": "1.1"}
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[110.0, -7.0], [110.01, -7.0], [110.01, -6.99], [110.0, -6.99], [110.0, -7.0]]],
}
SQUARE_2 = {
    "type": "Polygon",
    "coordinates": [[[110.0, -7.0], [110.02, -7.0], [110.02, -6.98], [110.0, -6.98], [110.0, -7.0]]],
}


@pytest.fixture()
def env():
    # Set DMRV_TEST_DATABASE_URL to a THROWAWAY PostgreSQL database to run these
    # tests on PostgreSQL; the tables are dropped afterwards. Default: SQLite.
    url = os.environ.get("DMRV_TEST_DATABASE_URL")
    if url:
        engine = create_engine(url)
        Base.metadata.drop_all(engine, tables=TABLES)
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as seed:
        seed.add(Role(id=1, name="limited"))
        seed.add_all([
            User(id=1, username="field-user", email="f@x.test", password_hash="x"),
            AdminUser(id=1, username="owner-a", password_hash="x"),
            AdminUser(id=2, username="owner-b", password_hash="x"),
            AdminUser(id=3, username="no-permission", password_hash="x", role_id=1),
        ])
        seed.commit()

    app = FastAPI()
    register_dmrv_error_handler(app)
    app.include_router(dmrv_routes.router)

    def get_test_db():
        with factory() as session:
            yield session

    def test_viewer(request: Request, db: Session = Depends(get_test_db)):
        kind, ident = request.headers["X-Test-Viewer"].split(":")
        return db.get(User if kind == "user" else AdminUser, int(ident))

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_current_app_viewer] = test_viewer
    client = TestClient(app, raise_server_exceptions=False)
    yield client, factory
    if url:
        Base.metadata.drop_all(engine, tables=TABLES)
    engine.dispose()


def call(client, method, url, viewer="admin:1", key=None, if_match=None, **kwargs):
    headers = {"X-Test-Viewer": viewer}
    if key:
        headers["Idempotency-Key"] = key
    if if_match:
        headers["If-Match"] = if_match
    return client.request(method, url, headers=headers, **kwargs)


def count(factory, model) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def create(client, viewer="admin:1", key="k-1", payload=PROJECT):
    return call(client, "POST", "/dmrv/projects", viewer=viewer, key=key, json=payload)


def test_create_project_adds_owner_and_audit_in_one_commit(env):
    client, factory = env
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["revision"] == 1 and body["my_role"] == "project_owner" and body["status"] == "draft"
    assert response.headers["ETag"] == '"1"'
    with factory() as db:
        member = db.execute(select(ProjectMember)).scalar_one()
        assert (member.principal_type, member.principal_id, member.role) == ("admin", 1, "project_owner")
        audit = db.execute(select(DmrvAuditEvent)).scalar_one()
        assert (audit.action, audit.actor_type, audit.actor_id, audit.project_id) == ("project.create", "admin", 1, body["id"])


def test_same_idempotency_key_same_body_returns_same_project(env):
    client, factory = env
    first = create(client)
    second = create(client)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert (count(factory, DmrvProject), count(factory, ProjectMember), count(factory, DmrvAuditEvent)) == (1, 1, 1)


def test_same_idempotency_key_different_body_is_422(env):
    client, factory = env
    create(client)
    response = create(client, payload={**PROJECT, "name": "Other"})
    assert response.status_code == 422
    assert response.json()["code"] == "idempotency_key_reused"
    assert count(factory, DmrvProject) == 1


def test_missing_idempotency_key_is_400(env):
    client, _ = env
    response = call(client, "POST", "/dmrv/projects", json=PROJECT)
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_required"


def test_patch_with_stale_if_match_is_412_and_changes_nothing(env):
    client, factory = env
    project_id = create(client).json()["id"]
    ok = call(client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"', json={"name": "Renamed"})
    assert ok.status_code == 200 and ok.json()["revision"] == 2 and ok.headers["ETag"] == '"2"'
    stale = call(client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"', json={"name": "Lost update"})
    assert stale.status_code == 412
    assert stale.json()["code"] == "revision_mismatch"
    with factory() as db:
        assert db.get(DmrvProject, project_id).name == "Renamed"
        updates = db.execute(select(func.count()).select_from(DmrvAuditEvent).where(DmrvAuditEvent.action == "project.update")).scalar_one()
        assert updates == 1


def test_patch_requires_if_match(env):
    client, _ = env
    project_id = create(client).json()["id"]
    response = call(client, "PATCH", f"/dmrv/projects/{project_id}", json={"name": "x"})
    assert response.status_code == 428


def test_user_and_admin_without_permission_cannot_create_project(env):
    client, factory = env
    assert create(client, viewer="user:1", key="a").status_code == 403
    assert create(client, viewer="admin:3", key="b").status_code == 403
    assert count(factory, DmrvProject) == 0


def test_null_role_admin_can_create_but_not_read_others_project(env):
    client, _ = env
    project_id = create(client, viewer="admin:1").json()["id"]
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="admin:2").status_code == 404
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="admin:1").status_code == 200


def test_add_member_rejects_unknown_role_and_unknown_account_then_grants_access(env):
    client, _ = env
    project_id = create(client).json()["id"]
    url = f"/dmrv/projects/{project_id}/members"
    bad_role = call(client, "POST", url, json={"principal_type": "user", "username": "field-user", "role": "king"})
    assert bad_role.status_code == 422 and bad_role.json()["code"] == "invalid_role"
    missing = call(client, "POST", url, json={"principal_type": "user", "username": "ghost", "role": "analyst"})
    assert missing.status_code == 422 and missing.json()["code"] == "unknown_account"
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="user:1").status_code == 404

    added = call(client, "POST", url, json={"principal_type": "user", "username": "field-user", "role": "field_operator"})
    assert added.status_code == 201 and added.json()["role"] == "field_operator"
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="user:1").status_code == 200
    again = call(client, "POST", url, json={"principal_type": "user", "username": "field-user", "role": "analyst"})
    assert again.status_code == 409


def test_boundary_versions_increment_and_old_versions_remain(env):
    client, _ = env
    project_id = create(client).json()["id"]
    url = f"/dmrv/projects/{project_id}/boundaries"
    first = call(client, "POST", url, json={"geojson": SQUARE, "reason": "initial"})
    second = call(client, "POST", url, json={"geojson": SQUARE_2, "reason": "revised after field check"})
    assert (first.status_code, second.status_code) == (201, 201)
    assert (first.json()["version"], second.json()["version"]) == (1, 2)
    listed = call(client, "GET", url).json()
    assert [item["version"] for item in listed] == [1, 2]
    assert listed[0]["checksum"] != listed[1]["checksum"]


def test_invalid_boundary_is_422_with_code(env):
    client, _ = env
    project_id = create(client).json()["id"]
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    response = call(client, "POST", f"/dmrv/projects/{project_id}/boundaries", json={"geojson": bowtie, "reason": "x"})
    assert response.status_code == 422 and response.json()["code"] == "invalid_geometry"


def test_audit_failure_rolls_back_project_creation(env, monkeypatch):
    client, factory = env

    def boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.services.dmrv.projects.record_audit", boom)
    response = create(client)
    assert response.status_code == 500
    assert (count(factory, DmrvProject), count(factory, ProjectMember), count(factory, DmrvIdempotencyKey)) == (0, 0, 0)


def test_dmrv_project_create_permission_is_seeded_for_admin_not_viewer():
    from scripts.seed_rbac import ADMIN_PERMISSIONS, VIEWER_PERMISSIONS

    assert "dmrv.project.create" in ADMIN_PERMISSIONS
    assert "dmrv.project.create" not in VIEWER_PERMISSIONS


def test_patch_with_null_name_is_422_not_500(env):
    client, factory = env
    project_id = create(client).json()["id"]
    response = call(client, "PATCH", f"/dmrv/projects/{project_id}", if_match='"1"', json={"name": None})
    assert response.status_code == 422
    with factory() as db:
        assert db.get(DmrvProject, project_id).revision == 1
