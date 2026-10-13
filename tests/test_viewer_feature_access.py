from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from app.core.security import ADMIN_SESSION_COOKIE, USER_SESSION_COOKIE, create_access_token, create_user_access_token
from app.core.viewer_features import (
    DEFAULT_VIEWER_FEATURE_CODES,
    VIEWER_FEATURE_CODES,
    ensure_feature_access,
    ensure_job_feature_access,
    ensure_workflow_feature_access,
    workflow_required_feature_codes,
)
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db


def _admin(role_name: str, permissions: set[str], role_id: int = 1):
    role = Role(
        id=role_id,
        name=role_name,
        permissions=[Permission(code=code) for code in permissions],
    )
    return AdminUser(
        id=1,
        username="test",
        password_hash="unused",
        is_active=True,
        role_id=role_id,
        role=role,
    )


def test_viewer_with_all_default_features_can_use_each_feature():
    viewer = _admin("viewer", set(DEFAULT_VIEWER_FEATURE_CODES))
    for code in VIEWER_FEATURE_CODES:
        ensure_feature_access(viewer, code, db=None)


def test_viewer_without_feature_is_denied_with_safe_403():
    viewer = _admin("viewer", {"app.feature.carbon_stock"})
    with pytest.raises(HTTPException) as exc_info:
        ensure_feature_access(viewer, "app.feature.disaster_mapping", db=None)

    assert exc_info.value.status_code == 403
    assert "secret" not in str(exc_info.value.detail).casefold()
    assert "credential" not in str(exc_info.value.detail).casefold()


def test_non_viewer_admin_keeps_existing_feature_access():
    limited_admin = _admin("admin", set(), role_id=2)
    ensure_feature_access(limited_admin, "app.feature.crop_monitoring", db=None)


@pytest.mark.parametrize(
    ("job_type", "feature"),
    [
        ("carbon", "app.feature.carbon_stock"),
        ("vegetation", "app.feature.vegetation_indices"),
        ("landcover_change", "app.feature.land_cover_change"),
        ("landcover_transition", "app.feature.land_cover_change"),
        ("crop_monitoring", "app.feature.crop_monitoring"),
    ],
)
def test_analysis_job_type_is_mapped_to_server_permission(job_type, feature):
    viewer = _admin("viewer", {feature})
    ensure_job_feature_access(viewer, job_type, db=None, payload={"permission": "app.feature.carbon_stock"})


def test_job_type_cannot_be_overridden_by_client_permission_payload():
    viewer = _admin("viewer", {"app.feature.carbon_stock"})
    with pytest.raises(HTTPException) as exc_info:
        ensure_job_feature_access(
            viewer,
            "vegetation",
            db=None,
            payload={"permission": "app.feature.carbon_stock", "type": "carbon"},
        )
    assert exc_info.value.status_code == 403


def test_workflow_feature_access_uses_server_category_and_node_types():
    viewer = _admin("viewer", {"app.feature.carbon_stock"})
    assert workflow_required_feature_codes({
        "category": "carbon",
        "nodes": [{"type": "carbon_model"}, {"type": "ndvi"}, {"type": "land_cover"}],
    }) == {
        "app.feature.carbon_stock",
        "app.feature.vegetation_indices",
        "app.feature.land_cover",
    }
    with pytest.raises(HTTPException) as exc_info:
        ensure_workflow_feature_access(
            viewer,
            {"category": "general", "nodes": [{"type": "ndvi"}]},
            db=None,
        )
    assert exc_info.value.status_code == 403

    full_viewer = _admin("viewer", set(DEFAULT_VIEWER_FEATURE_CODES))
    with pytest.raises(HTTPException) as unknown_node:
        ensure_workflow_feature_access(
            full_viewer,
            {"category": "general", "nodes": [{"type": "unknown_action"}]},
            db=None,
        )
    assert unknown_node.value.status_code == 403


def test_reseed_preserves_an_intentionally_empty_viewer_feature_selection(monkeypatch):
    from scripts import seed_rbac

    engine = create_engine("sqlite://")
    Role.__table__.create(engine)
    Permission.__table__.create(engine)
    role_permissions.create(engine)
    local_sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(seed_rbac, "SessionLocal", local_sessions)

    try:
        seed_rbac.seed_rbac()
        with local_sessions() as db:
            viewer = db.query(Role).filter_by(name="viewer").one()
            assert VIEWER_FEATURE_CODES.issubset({permission.code for permission in viewer.permissions})
            admin = db.query(Role).filter_by(name="admin").one()
            expert = db.query(Role).filter_by(name="geospatial_expert").one()
            assert "viewer_features.manage" in {permission.code for permission in admin.permissions}
            assert "viewer_features.manage" not in {permission.code for permission in viewer.permissions}
            assert "viewer_features.manage" not in {permission.code for permission in expert.permissions}
            assert not ({"credential.read", "credential.write", "secret.read", "secret.write"} & {permission.code for permission in admin.permissions})
            viewer.permissions = [p for p in viewer.permissions if p.code not in VIEWER_FEATURE_CODES]
            viewer.feature_permissions_configured = True
            db.commit()

        seed_rbac.seed_rbac()
        with local_sessions() as db:
            viewer = db.query(Role).filter_by(name="viewer").one()
            selected = {permission.code for permission in viewer.permissions}
            assert not (selected & VIEWER_FEATURE_CODES)
            assert viewer.feature_permissions_configured is True
    finally:
        engine.dispose()


def test_viewer_without_role_features_fails_closed_when_role_lookup_is_empty():
    from types import SimpleNamespace
    from app.core.viewer_features import viewer_feature_codes

    class EmptyQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    class EmptyDb:
        def query(self, _model):
            return EmptyQuery()

    assert viewer_feature_codes(EmptyDb(), SimpleNamespace(id=22, username="public-user")) == set()


def test_viewer_api_gates_alternate_user_auth_and_analysis_payload(monkeypatch):
    from app.main import app

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Role.__table__.create(engine)
    Permission.__table__.create(engine)
    role_permissions.create(engine)
    AdminUser.__table__.create(engine)
    User.__table__.create(engine)
    from app.db.models.workflow import Workflow
    from app.db.models.system_config import SystemConfig
    Workflow.__table__.create(engine)
    SystemConfig.__table__.create(engine)
    local_sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with local_sessions() as db:
        carbon = Permission(code="app.feature.carbon_stock", description="carbon")
        disaster = Permission(code="app.feature.disaster_mapping", description="disaster")
        viewer_role = Role(name="viewer", feature_permissions_configured=True, permissions=[carbon])
        db.add_all([viewer_role, disaster])
        db.flush()
        admin = AdminUser(
            username="viewer-admin-test",
            password_hash="unused",
            is_active=True,
            role_id=viewer_role.id,
        )
        public_user = User(
            username="viewer-public-test",
            email="viewer-public-test@example.test",
            password_hash="unused",
            is_active=True,
        )
        db.add_all([admin, public_user])
        db.commit()
        restricted_workflow = Workflow(
            owner_id=public_user.id,
            name="Restricted disaster workflow",
            category="disaster",
            visibility="public",
            workflow_json={
                "category": "disaster",
                "nodes": [{"id": "event", "type": "hotspot_3d"}],
            },
            checksum="0" * 64,
        )
        db.add(restricted_workflow)
        db.commit()
        restricted_workflow_id = restricted_workflow.id
        admin_token = create_access_token(admin)
        user_token = create_user_access_token(public_user)

    def override_get_db():
        db = local_sessions()
        try:
            yield db
        finally:
            db.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, headers={"Origin": "http://localhost:5501"})
    try:
        for cookie_name, token in ((ADMIN_SESSION_COOKIE, admin_token), (USER_SESSION_COOKIE, user_token)):
            response = client.get("/api/app/features", cookies={cookie_name: token})
            assert response.status_code == 200
            flags = {item["code"]: item["enabled"] for item in response.json()["features"]}
            assert flags["app.feature.carbon_stock"] is True
            assert flags["app.feature.vegetation_indices"] is False

        anonymous_features = client.get("/api/app/features")
        assert anonymous_features.status_code == 401

        anonymous_map_layers = client.get("/api/map-layers")
        assert anonymous_map_layers.status_code == 401
        carbon_layers = client.get(
            "/api/map-layers?module=carbon",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert carbon_layers.status_code == 200
        denied_vegetation_layers = client.get(
            "/api/map-layers?module=vegetation",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert denied_vegetation_layers.status_code == 403
        unknown_map_module = client.get(
            "/api/map-layers?module=unmapped-module",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert unknown_map_module.status_code == 403
        shared_basemaps = client.get("/api/basemaps")
        assert shared_basemaps.status_code == 200

        denied = client.get(
            "/api/vegetation/catalog",
            cookies={ADMIN_SESSION_COOKIE: admin_token},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"].startswith("Akses ke fitur ini")

        denied_catalog = client.get(
            "/api/landcover/datasets",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert denied_catalog.status_code == 403
        denied_legacy_landcover = client.get(
            "/api/datasets?module=landcover",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert denied_legacy_landcover.status_code == 403
        denied_unmapped_legacy_dataset = client.get(
            "/api/datasets?module=unknown-module",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert denied_unmapped_legacy_dataset.status_code == 403
        denied_geospatial_dataset = client.get(
            "/api/geospatial/datasets?module=disaster",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert denied_geospatial_dataset.status_code == 403

        from app.api.routes import geospatial
        carbon_dataset_id = "viewer-carbon-access-test"
        disaster_dataset_id = "viewer-disaster-access-test"
        monkeypatch.setitem(geospatial._catalog, carbon_dataset_id, {
            "id": carbon_dataset_id, "name": "Carbon test", "format": "cog", "module": "carbon",
        })
        monkeypatch.setitem(geospatial._catalog, disaster_dataset_id, {
            "id": disaster_dataset_id, "name": "Disaster test", "format": "cog", "module": "disaster",
        })
        dataset_allowed = client.get(
            f"/api/geospatial/datasets/{carbon_dataset_id}",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert dataset_allowed.status_code == 200
        unknown_dataset_module = client.get(
            "/api/geospatial/datasets?module=unmapped-module",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert unknown_dataset_module.status_code == 403
        export = client.post(
            "/api/geospatial/export/cog",
            cookies={USER_SESSION_COOKIE: user_token},
            json={"datasetId": carbon_dataset_id},
        )
        assert export.status_code == 202
        geospatial_job_id = export.json()["id"]
        assert client.get(
            f"/api/geospatial/jobs/{geospatial_job_id}",
            cookies={USER_SESSION_COOKIE: user_token},
        ).status_code == 200

        with local_sessions() as db:
            role = db.query(Role).filter_by(name="viewer").one()
            disaster = db.query(Permission).filter_by(code="app.feature.disaster_mapping").one()
            role.permissions = [disaster]
            role.feature_permissions_configured = True
            db.commit()
        assert client.get(
            f"/api/geospatial/datasets/{carbon_dataset_id}",
            cookies={USER_SESSION_COOKIE: user_token},
        ).status_code == 403
        for path, method in (
            (f"/api/geospatial/jobs/{geospatial_job_id}", "get"),
            (f"/api/geospatial/jobs/{geospatial_job_id}/cancel", "post"),
        ):
            response = getattr(client, method)(path, cookies={USER_SESSION_COOKIE: user_token})
            assert response.status_code == 403
        disaster_export = client.post(
            "/api/geospatial/export/cog",
            cookies={USER_SESSION_COOKIE: user_token},
            json={"datasetId": disaster_dataset_id},
        )
        assert disaster_export.status_code == 202
        disaster_job_id = disaster_export.json()["id"]

        with local_sessions() as db:
            role = db.query(Role).filter_by(name="viewer").one()
            carbon = db.query(Permission).filter_by(code="app.feature.carbon_stock").one()
            role.permissions = [carbon]
            db.commit()

        job_denied = client.post(
            "/api/analysis-jobs",
            cookies={ADMIN_SESSION_COOKIE: admin_token},
            json={
                "type": "vegetation",
                "payload": {"permission": "app.feature.carbon_stock", "type": "carbon"},
            },
        )
        assert job_denied.status_code == 403

        workflow_denied = client.get(
            f"/api/workflows/{restricted_workflow_id}",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert workflow_denied.status_code == 403
        anonymous_workflow = client.get(f"/api/workflows/{restricted_workflow_id}")
        assert anonymous_workflow.status_code == 401
        visible_workflows = client.get(
            "/api/workflows",
            cookies={USER_SESSION_COOKIE: user_token},
        )
        assert visible_workflows.status_code == 200
        assert visible_workflows.json()["items"] == []
        workflow_create_denied = client.post(
            "/api/workflows",
            cookies={USER_SESSION_COOKIE: user_token},
            json={
                "workflow": {
                    "name": "Forbidden disaster workflow",
                    "category": "disaster",
                    "nodes": [
                        {"id": "event", "type": "hotspot_3d", "label": "Hotspot", "position": {"x": 0, "y": 0}}
                    ],
                }
            },
        )
        assert workflow_create_denied.status_code == 403
    finally:
        from app.api.routes import geospatial
        geospatial._jobs.pop(locals().get("geospatial_job_id", ""), None)
        geospatial._jobs.pop(locals().get("disaster_job_id", ""), None)
        client.close()
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        engine.dispose()


def test_narrow_viewer_manager_and_full_access_can_update_without_sensitive_access(monkeypatch):
    from app.main import app
    from app.api.routes import admin as admin_routes

    audit_events = []
    monkeypatch.setattr(
        admin_routes.audit_service,
        "log_audit",
        lambda _db, _actor_id, action, resource_type, resource_id, detail=None, ip_address=None: audit_events.append(
            {"action": action, "resource_type": resource_type, "resource_id": resource_id, "detail": detail}
        ),
    )

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Role.__table__.create(engine)
    Permission.__table__.create(engine)
    role_permissions.create(engine)
    AdminUser.__table__.create(engine)
    local_sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with local_sessions() as db:
        viewer_manager = Permission(code="viewer_features.manage", description="manage Viewer feature config")
        old_broad_manager = Permission(code="roles.manage", description="unrelated legacy permission")
        feature_permissions = {
            code: Permission(code=code, description=code)
            for code in VIEWER_FEATURE_CODES
        }
        carbon = feature_permissions["app.feature.carbon_stock"]
        viewer = Role(name="viewer", feature_permissions_configured=True, permissions=[carbon])
        limited_role = Role(name="admin", permissions=[viewer_manager])
        old_broad_role = Role(name="geospatial_expert", permissions=[old_broad_manager])
        db.add_all([viewer_manager, old_broad_manager, *feature_permissions.values(), viewer, limited_role, old_broad_role])
        db.flush()
        limited = AdminUser(
            username="limited-admin-test",
            password_hash="unused",
            is_active=True,
            role_id=limited_role.id,
        )
        full_access = AdminUser(
            username="full-access-admin-test",
            password_hash="unused",
            is_active=True,
            role_id=None,
        )
        viewer_admin = AdminUser(
            username="viewer-admin-test",
            password_hash="unused",
            is_active=True,
            role_id=viewer.id,
        )
        broad_only = AdminUser(
            username="broad-only-test",
            password_hash="unused",
            is_active=True,
            role_id=old_broad_role.id,
        )
        db.add_all([limited, full_access, viewer_admin, broad_only])
        db.commit()
        limited_token = create_access_token(limited)
        full_token = create_access_token(full_access)
        viewer_token = create_access_token(viewer_admin)
        broad_token = create_access_token(broad_only)

    def override_get_db():
        db = local_sessions()
        try:
            yield db
        finally:
            db.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, headers={"Origin": "http://localhost:5501"})
    try:
        legacy_permission_denied = client.put(
            "/api/admin/roles/viewer-features",
            cookies={ADMIN_SESSION_COOKIE: broad_token},
            json={"enabled_features": []},
        )
        assert legacy_permission_denied.status_code == 403
        viewer_denied = client.get(
            "/api/admin/roles/viewer-features",
            cookies={ADMIN_SESSION_COOKIE: viewer_token},
        )
        assert viewer_denied.status_code == 403
        sensitive_denied = client.get(
            "/api/admin/gee/credentials",
            cookies={ADMIN_SESSION_COOKIE: limited_token},
        )
        assert sensitive_denied.status_code == 403
        key_pool_denied = client.get(
            "/api/admin/key-pool/status",
            cookies={ADMIN_SESSION_COOKIE: limited_token},
        )
        assert key_pool_denied.status_code == 403
        secret_write_denied = client.put(
            "/api/admin/arcgis/config",
            cookies={ADMIN_SESSION_COOKIE: limited_token},
            json={"enabled": False},
        )
        assert secret_write_denied.status_code == 403
        assert client.get(
            "/api/admin/roles/viewer-features",
            cookies={ADMIN_SESSION_COOKIE: limited_token},
        ).status_code == 200
        invalid_codes = client.put(
            "/api/admin/roles/viewer-features",
            cookies={ADMIN_SESSION_COOKIE: limited_token},
            json={"enabled_features": ["app.feature.carbon_stock", "unknown.feature"]},
        )
        assert invalid_codes.status_code == 422

        saved_by_limited = client.put(
            "/api/admin/roles/viewer-features",
            cookies={ADMIN_SESSION_COOKIE: limited_token},
            json={"enabled_features": []},
        )
        assert saved_by_limited.status_code == 200
        assert all(feature["enabled"] is False for feature in saved_by_limited.json()["features"])

        saved = client.put(
            "/api/admin/roles/viewer-features",
            cookies={ADMIN_SESSION_COOKIE: full_token},
            json={"enabled_features": ["app.feature.carbon_stock"]},
        )
        assert saved.status_code == 200
        with local_sessions() as db:
            viewer = db.query(Role).filter_by(name="viewer").one()
            assert {p.code for p in viewer.permissions} & VIEWER_FEATURE_CODES == {"app.feature.carbon_stock"}
            assert viewer.feature_permissions_configured is True
            admin_role = db.query(Role).filter_by(name="admin").one()
            assert "viewer_features.manage" in {p.code for p in admin_role.permissions}
            assert not ({"credential.read", "credential.write", "secret.read", "secret.write"} & {p.code for p in admin_role.permissions})
        assert len(audit_events) == 2
        assert all(event["action"] == "role.viewer_features.update" for event in audit_events)
        assert all(event["resource_type"] == "role" for event in audit_events)
        assert all(set(event["detail"]) == {"enabled_features"} for event in audit_events)
        assert all(not (set(event["detail"]) & {"secret", "credential", "api_key", "password"}) for event in audit_events)
    finally:
        client.close()
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        engine.dispose()
