"""Workspace contracts on SQLite and an explicitly supplied throwaway PostgreSQL.

PG tests use a unique schema, not public tables. No production database URL is
inferred from app settings. Set WORKSPACE_TEST_DATABASE_URL explicitly.
"""
import datetime as dt
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.pool import StaticPool

from app.api.routes import admin, admin_mfa, dmrv, organizations, workspace_analysis
from app.core.security import get_current_app_viewer
from app.core.viewer_features import DEFAULT_VIEWER_FEATURE_CODES
from app.db.base import Base
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_audit import DmrvAuditEvent, DmrvIdempotencyKey
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.dmrv_integrity import DmrvIntegrityDocument, DmrvIntegrityRecord
from app.db.models.dmrv_advanced import DmrvFieldCapture
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.workspace_analysis import WorkspaceAnalysisRun, WorkspaceQuota, WorkspaceUsageEvent
from app.db.models.organization import Organization, OrganizationInvitation, OrganizationMember, WorkspaceRequest
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.user import User
from app.db.models.gee_credential import GEECredential
from app.db.models.audit_log import AuditLog
from app.db.models.admin_mfa import AdminMfa, MfaChallenge
from app.db.models.carbon_calibration import CarbonCalibrationDataset, CarbonCalibrationFile, CarbonCalibrationRun, CarbonCalibrationModel, FieldPlot
from app.db.session import get_db
from app.schemas.organizations import MemberChange
from app.services import organizations as service
from app.services.dmrv.authz import Principal
from app.services.dmrv.errors import DmrvError, register_dmrv_error_handler


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


TABLES = [User.__table__, AdminUser.__table__, Role.__table__, Permission.__table__, role_permissions,
          Organization.__table__, OrganizationMember.__table__, OrganizationInvitation.__table__, WorkspaceRequest.__table__,
          DmrvProject.__table__, ProjectMember.__table__, DmrvAuditEvent.__table__, DmrvIdempotencyKey.__table__,
          BoundaryVersion.__table__, DmrvMonitoringPeriod.__table__, DmrvJob.__table__, DmrvSnapshot.__table__,
          DmrvIntegrityRecord.__table__, DmrvIntegrityDocument.__table__, ImportBatch.__table__, SurveyPlot.__table__,
          SurveyVisit.__table__, TreeMeasurement.__table__, DmrvFieldCapture.__table__,
          WorkspaceAnalysisRun.__table__, WorkspaceQuota.__table__, WorkspaceUsageEvent.__table__, GEECredential.__table__, AuditLog.__table__,
          AdminMfa.__table__, MfaChallenge.__table__, CarbonCalibrationDataset.__table__, CarbonCalibrationFile.__table__,
          CarbonCalibrationRun.__table__, CarbonCalibrationModel.__table__, FieldPlot.__table__]


@pytest.fixture(params=["sqlite", "postgresql"])
def env(request):
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("WORKSPACE_TEST_DATABASE_URL")
        if not url:
            pytest.skip("Explicit WORKSPACE_TEST_DATABASE_URL required for PostgreSQL isolation tests")
        root = create_engine(url)
        schema = "workspace_test_" + uuid.uuid4().hex
        with root.begin() as conn:
            conn.execute(CreateSchema(schema))
        engine = root.execution_options(schema_translate_map={None: schema})
    else:
        root = engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        viewer_role = Role(
            name="viewer",
            is_viewer=True,
            feature_permissions_configured=True,
        )
        viewer_role.permissions = [Permission(code=code) for code in DEFAULT_VIEWER_FEATURE_CODES]
        db.add(viewer_role)
        db.add_all([User(id=1, username="alice", email="alice@example.test", password_hash="x"),
                    User(id=2, username="bob", email="bob@example.test", password_hash="x"),
                    User(id=3, username="carol", email="carol@example.test", password_hash="x"),
                    AdminUser(id=1, is_superuser=True, username="root-admin", email="root@example.test", password_hash="x"),
                    AdminUser(id=2, is_superuser=True, username="reviewer", email="reviewer@example.test", password_hash="x")])
        db.commit()
    app = FastAPI()
    register_dmrv_error_handler(app)
    app.include_router(organizations.router)
    app.include_router(dmrv.router)
    app.include_router(workspace_analysis.router)
    app.include_router(admin.router)
    app.include_router(admin_mfa.router)

    def session():
        with factory() as db:
            yield db

    def viewer(req: Request, db=Depends(session)):
        kind, ident = req.headers.get("X-Viewer", "user:1").split(":")
        return db.get(User if kind == "user" else AdminUser, int(ident))

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_app_viewer] = viewer
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, factory, request.param
    if schema:
        with root.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
    root.dispose()


def call(client, method, path, viewer="user:1", **kwargs):
    return client.request(method, path, headers={"X-Viewer": viewer, "Idempotency-Key": kwargs.pop("key", uuid.uuid4().hex)}, **kwargs)


def workspace(client, viewer="user:1", key=None, name="Research"):
    result = call(client, "POST", "/organizations", viewer, key=key or uuid.uuid4().hex, json={"name": name})
    assert result.status_code == 201, result.text
    return result.json()["id"]


def invite(client, org, email="bob@example.test", role="member"):
    result = call(client, "POST", f"/organizations/{org}/invitations", json={"email": email, "role": role})
    assert result.status_code == 201, result.text
    assert result.headers["cache-control"] == "no-store"
    return result.json()


def test_create_replay_and_owner_isolation(env):
    client, factory, _ = env
    org = workspace(client, key="same")
    assert workspace(client, key="same") == org
    assert call(client, "POST", "/organizations", key="same", json={"name": "Other"}).status_code == 409
    assert call(client, "GET", "/organizations", "user:2").json() == []
    assert call(client, "GET", f"/organizations/{org}/members", "admin:1").status_code == 404
    assert call(client, "GET", f"/organizations/{org}/members", "user:2").status_code == 404
    with factory() as db:
        assert len(list(db.scalars(select(Organization)))) == 1


def test_invitation_email_expiry_revoke_and_replay(env):
    client, factory, _ = env
    org = workspace(client)
    invitation = invite(client, org)
    token = invitation["token"]
    with factory() as db:
        stored = db.get(OrganizationInvitation, invitation["invitation"]["id"])
        assert stored.token_hash != token and len(stored.token_hash) == 64
        assert token not in str([row.after for row in db.scalars(select(DmrvAuditEvent))])
    assert token not in call(client, "GET", f"/organizations/{org}/invitations").text
    assert call(client, "POST", "/organizations/invitations/accept", "user:3", json={"token": token}).status_code == 404
    assert call(client, "POST", "/organizations/invitations/accept", "user:2", json={"token": token}).status_code == 200
    assert call(client, "POST", "/organizations/invitations/accept", "user:2", json={"token": token}).status_code == 404
    assert call(client, "GET", f"/organizations/{org}/members", "user:2").status_code == 403
    expired = invite(client, org, "carol@example.test")
    with factory() as db:
        db.get(OrganizationInvitation, expired["invitation"]["id"]).expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        db.commit()
    assert call(client, "POST", "/organizations/invitations/accept", "user:3", json={"token": expired["token"]}).status_code == 404
    revoked = invite(client, org, "carol@example.test")
    assert call(client, "DELETE", f"/organizations/{org}/invitations/{revoked['invitation']['id']}").status_code == 204
    assert call(client, "POST", "/organizations/invitations/accept", "user:3", json={"token": revoked["token"]}).status_code == 404


def test_last_owner_and_tenant_membership_required_for_projects(env):
    client, factory, _ = env
    org = workspace(client)
    owner = call(client, "GET", f"/organizations/{org}/members").json()[0]
    assert call(client, "PATCH", f"/organizations/{org}/members/{owner['id']}", json={"role": "member"}).status_code == 409
    project = call(client, "POST", "/dmrv/projects", json={"name": "Pilot", "organization_id": org})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    assert project.json()["organization_id"] == org
    assert call(client, "GET", f"/dmrv/projects/{project_id}", "admin:1").status_code == 404
    assert call(client, "POST", "/dmrv/projects", "user:2", json={"name": "Foreign", "organization_id": org}).status_code == 404
    assert call(client, "POST", f"/dmrv/projects/{project_id}/members", json={"principal_type": "user", "username": "bob", "role": "analyst"}).status_code == 404
    invitation = invite(client, org)
    assert call(client, "POST", "/organizations/invitations/accept", "user:2", json={"token": invitation["token"]}).status_code == 200
    assert call(client, "POST", f"/dmrv/projects/{project_id}/members", json={"principal_type": "user", "username": "bob", "role": "analyst"}).status_code == 201
    assert call(client, "GET", f"/dmrv/projects/{project_id}", "user:2").status_code == 200
    member = next(m for m in call(client, "GET", f"/organizations/{org}/members").json() if m["username"] == "bob")
    assert call(client, "PATCH", f"/organizations/{org}/members/{member['id']}", json={"role": "member", "is_active": False}).status_code == 200
    assert call(client, "GET", f"/dmrv/projects/{project_id}", "user:2").status_code == 404
    assert call(client, "GET", "/dmrv/projects", "user:2").json() == []


def test_invite_cannot_reactivate_or_escalate_member(env):
    client, _, _ = env
    org = workspace(client)
    inv = invite(client, org, role="admin")
    assert call(client, "POST", "/organizations/invitations/accept", "user:2", json={"token": inv["token"]}).status_code == 200
    owner = next(m for m in call(client, "GET", f"/organizations/{org}/members").json() if m["username"] == "alice")
    assert call(client, "PATCH", f"/organizations/{org}/members/{owner['id']}", "user:2", json={"role": "member"}).status_code == 403
    assert call(client, "POST", f"/organizations/{org}/invitations", "user:2", json={"email": "carol@example.test", "role": "admin"}).status_code == 403


def test_concurrent_owners_cannot_remove_each_other(env):
    client, factory, engine_type = env
    if engine_type != "postgresql":
        pytest.skip("Row-lock concurrency requires PostgreSQL")
    org = workspace(client)
    inv = invite(client, org)
    call(client, "POST", "/organizations/invitations/accept", "user:2", json={"token": inv["token"]})
    members = call(client, "GET", f"/organizations/{org}/members").json()
    alice = next(m for m in members if m["username"] == "alice")
    bob = next(m for m in members if m["username"] == "bob")
    assert call(client, "PATCH", f"/organizations/{org}/members/{bob['id']}", json={"role": "owner"}).status_code == 200
    barrier = threading.Barrier(2)

    def demote(actor_id, target_id):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                service.change_member(db, org, target_id, Principal("user", actor_id), MemberChange(role="member"))
                return "ok"
            except DmrvError:
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(demote, 1, bob["id"])
        b = executor.submit(demote, 2, alice["id"])
        results = [a.result(timeout=15), b.result(timeout=15)]
    assert sorted(results) == ["blocked", "ok"]
    with factory() as db:
        assert len(list(db.scalars(select(OrganizationMember).where(OrganizationMember.organization_id == org,
            OrganizationMember.role == "owner", OrganizationMember.is_active.is_(True))))) == 1
