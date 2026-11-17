"""Regression evidence for the signed static review, evaluated on current code."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import fields, geospatial, user_auth
from app.core.security import create_user_access_token
from app.db.models.field import Field
from app.db.models.user import User
from app.db.session import get_db
from app.providers import arcgis_client
from app.services import crop_monitoring_service
from app.services.gee_common import AnalysisError
from app.core.client_ip import get_client_ip
from app.core.config import Settings
from app.core.http import SecurityHeadersMiddleware
from starlette.requests import Request


@pytest.fixture
def field_api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    User.__table__.create(engine)
    Field.__table__.create(engine)
    app = FastAPI()
    app.include_router(fields.router, prefix="/api")
    app.dependency_overrides[fields.router.dependencies[0].dependency] = lambda: None
    def db_dependency():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = db_dependency
    with Session(engine) as db:
        users = [User(id=i, username=f"audit-{i}", email=f"audit-{i}@example.test", password_hash="unused", is_active=True) for i in (1, 2)]
        db.add_all(users)
        db.commit()
        tokens = [create_user_access_token(user) for user in users]
        db.add(Field(name="Legacy", owner_key=None, geojson={}, area_ha=1, commodity="rice"))
        db.commit()
    with TestClient(app) as client:
        yield client, tokens, engine
    engine.dispose()


def test_fields_require_login_and_isolate_read_update_delete_and_analysis(field_api):
    client, tokens, engine = field_api
    a, b = [{"Authorization": f"Bearer {token}"} for token in tokens]
    for method, path in [("get", "/api/fields"), ("get", "/api/fields/1"), ("delete", "/api/fields/1")]:
        assert getattr(client, method)(path).status_code == 401
    payload = {"name": "Private", "commodity": "padi", "geojson": {"type": "Polygon", "coordinates": [[[110,-7],[111,-7],[111,-6],[110,-7]]]}}
    created = client.post("/api/fields", json=payload, headers=a)
    assert created.status_code == 200, created.text
    field_id = created.json()["id"]
    assert client.get("/api/fields", headers=b).json()["fields"] == []
    assert [row["id"] for row in client.get("/api/fields", headers=a).json()["fields"]] == [field_id]
    assert client.get(f"/api/fields/{field_id}", headers=b).status_code == 404
    assert client.patch(f"/api/fields/{field_id}", json={"name": "stolen"}, headers=b).status_code == 404
    assert client.delete(f"/api/fields/{field_id}", headers=b).status_code == 404
    with Session(engine) as db:
        assert db.get(Field, field_id).name == "Private"
        assert db.query(Field).filter_by(owner_key=None).count() == 1
        assert fields.field_repo.list_fields(db, None) == []
        for owner in ("user:2", "admin:1", None):
            with pytest.raises(AnalysisError) as error:
                crop_monitoring_service.run_crop_monitoring(db, {"field_id": field_id}, owner)
            assert error.value.status_code == 404
    assert client.patch(f"/api/fields/{field_id}", json={"name": "Updated"}, headers=a).status_code == 200
    assert client.delete(f"/api/fields/{field_id}", headers=a).status_code == 200


def test_field_migration_preserves_existing_data():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0052_field_ownership.py"
    spec = importlib.util.spec_from_file_location("field_ownership_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE fields (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO fields VALUES (1, 'Keep this boundary')"))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        assert conn.execute(text("SELECT id, name, owner_key FROM fields")).one() == (1, "Keep this boundary", None)
        migration.downgrade()
        assert conn.execute(text("SELECT id, name FROM fields")).one() == (1, "Keep this boundary")
        migration.upgrade()
    engine.dispose()


@pytest.mark.parametrize("sql", ["SELECT * FROM scenes", "WITH x AS (SELECT * FROM admin_users) SELECT * FROM x", "SELECT 1 UNION SELECT 1", "SELECT * FROM (SELECT 1) s", "SELECT pg_sleep(30)"])
def test_server_sql_creates_no_jobs(sql):
    with pytest.raises(HTTPException) as error:
        geospatial.geospatial_query(geospatial.QueryRequest(datasetId="scene", sql=sql), viewer=User(id=1), db=None)
    assert error.value.status_code == 501
    assert not hasattr(geospatial, "_jobs")


def test_registration_closed_by_default_without_touching_database(monkeypatch):
    monkeypatch.setattr(user_auth, "get_settings", lambda: SimpleNamespace(allow_user_registration=False))
    app = FastAPI()
    app.include_router(user_auth.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        assert client.get("/auth/config").json() == {"registration_enabled": False}
        response = client.post("/auth/register", json={"username": "new-user", "email": "new@example.test", "password": "AuditPassword123!"})
        assert response.status_code == 404
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize("url", ["https://www.arcgis.com:443@evil.example/path", "https://www.arcgis.com@evil.example/path", "http://www.arcgis.com/path", "https://www.arcgis.com.evil.example/path", "https://www.arcgis.com:8443/path", "https://user:pass@www.arcgis.com/path"])
def test_arcgis_domain_parser_rejects_bypasses(url):
    assert not arcgis_client._is_allowed_domain(url)


def test_arcgis_client_refuses_redirects_even_with_provider_credentials(monkeypatch):
    calls = []
    def redirect(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=302)
    monkeypatch.setattr(arcgis_client.requests, "get", redirect)
    client = arcgis_client.ArcGISClient()
    client.configure(enabled=True, portal_url="https://www.arcgis.com", auth_mode="api_key", api_key="test-placeholder", timeout=1)
    for action in (lambda: client.get_item_metadata("abc123"), lambda: client.get_service_metadata("https://services.arcgis.com/demo/ImageServer"), client._fetch_portal_info):
        with pytest.raises(ValueError, match="redirects"):
            action()
    assert len(calls) == 3
    assert all(call["allow_redirects"] is False for call in calls)


def test_client_ip_and_rate_limits_ignore_spoofed_untrusted_forwarding_headers():
    settings = Settings(_env_file=None, trusted_proxy_cidrs="10.0.0.0/24", rate_limit_requests_per_minute=1)
    def request(peer, forwarded):
        return Request({"type": "http", "method": "GET", "path": "/api/data", "headers": [(b"x-forwarded-for", forwarded.encode())], "client": (peer, 123)})
    assert get_client_ip(request("198.51.100.5", "1.2.3.4"), settings) == "198.51.100.5"
    assert get_client_ip(request("10.0.0.2", "1.2.3.4, 198.51.100.5, 10.0.0.3"), settings) == "198.51.100.5"
    assert get_client_ip(request("10.0.0.2", "malformed"), settings) == "10.0.0.2"
    limiter = SecurityHeadersMiddleware(lambda *_: None, settings)
    assert not limiter._rate_limited(request("10.0.0.2", "198.51.100.5"))
    assert limiter._rate_limited(request("10.0.0.2", "198.51.100.5"))
    assert not limiter._rate_limited(request("10.0.0.2", "198.51.100.6"))


def test_cv_scaler_learns_only_training_rows_before_final_fit(monkeypatch):
    import numpy as np
    from app.inference import carbon_model
    lengths = []
    original = carbon_model.StandardScaler
    class RecordingScaler(original):
        def fit(self, X, y=None, sample_weight=None):
            lengths.append(len(X))
            return super().fit(X, y, sample_weight=sample_weight)
    monkeypatch.setattr(carbon_model, "StandardScaler", RecordingScaler)
    model = carbon_model.CarbonEstimationModel(algorithm="ridge")
    X = np.random.default_rng(7).normal(size=(50, 3))
    y = X[:, 0] * 2 + X[:, 1]
    model.train(X, y, ["a", "b", "c"], cv_folds=5)
    assert lengths == [40, 40, 40, 40, 40, 50]
    assert model.metadata["cv_method"] == "kfold_train_only_scaler_v1"
    assert np.isfinite(model.predict(X)).all()


@pytest.mark.parametrize("name", ["../escape.tif", "/absolute.tif", "folder/../../escape.tif", "C:/escape.tif", "folder\\escape.tif"])
def test_geotiff_zip_rejects_unsafe_member_paths(tmp_path, name):
    import zipfile
    from app.api.routes.admin_disaster import _select_zip_geotiff
    path = tmp_path / "upload.zip"
    with zipfile.ZipFile(path, "w") as archive:
        member = zipfile.ZipInfo("placeholder.tif")
        member.filename = name  # write the exact hostile name on Windows too
        archive.writestr(member, b"not a raster")
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        _select_zip_geotiff(path)


def test_geotiff_zip_accepts_a_nested_raster(tmp_path):
    import zipfile
    from app.api.routes.admin_disaster import _select_zip_geotiff
    path = tmp_path / "upload.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("scene/image_ortho.tif", b"raster placeholder")
    assert _select_zip_geotiff(path).endswith("/scene/image_ortho.tif")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_vertical_conversion_rejects_nonfinite_values_in_schema_and_service(value):
    from pydantic import ValidationError
    from app.api.routes.vertical import ElevationConversionRequest
    from app.services.vertical_datum_service import convert_ellipsoidal_to_orthometric
    payload = {"ellipsoidal_height_m": value, "geoid_undulation_m": 10, "source_vertical_reference": "ELLIPSOIDAL"}
    with pytest.raises(ValidationError):
        ElevationConversionRequest(**payload)
    with pytest.raises(ValueError, match="finite"):
        convert_ellipsoidal_to_orthometric(**payload)


@pytest.mark.asyncio
async def test_slow_analysis_does_not_block_the_event_loop(monkeypatch):
    import asyncio
    import threading
    from app.api.routes import vegetation
    started = threading.Event()
    release = threading.Event()
    main_thread = threading.get_ident()
    def slow(*_):
        assert threading.get_ident() != main_thread
        started.set()
        release.wait(timeout=3)
        return {"ok": True}
    async def body():
        return {}
    monkeypatch.setattr(vegetation.vegetation_service, "analyze_vegetation", slow)
    task = asyncio.create_task(vegetation.analyze_vegetation(SimpleNamespace(json=body), db=None))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not task.done()
    finally:
        release.set()
    assert await task == {"ok": True}


@pytest.mark.parametrize("path", ["/regions/children-geometries?parent_code=33&child_endpoint=city&limit=999999", "/regions/geometry?endpoint=province&code=../../private"])
def test_region_api_rejects_excessive_limits_and_malformed_codes(path):
    from app.api.routes import regions
    app = FastAPI()
    app.include_router(regions.router)
    with TestClient(app) as client:
        assert client.get(path).status_code == 422


@pytest.mark.asyncio
async def test_global_error_handlers_hide_exception_and_validation_inputs():
    import json
    from app.main import http_exception_handler, validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    request = Request({"type": "http", "method": "POST", "path": "/api/test", "headers": [], "state": {"request_id": "audit-safe-error"}})
    response = await http_exception_handler(request, HTTPException(503, "postgresql://private-password@internal-db"))
    assert "private-password" not in response.body.decode()
    assert json.loads(response.body)["request_id"] == "audit-safe-error"
    validation = RequestValidationError([{"loc": ("body", "password"), "type": "value_error", "msg": "private-password", "input": "private-password", "ctx": {"secret": "private-password"}}])
    response = await validation_exception_handler(request, validation)
    assert "private-password" not in response.body.decode()
    assert json.loads(response.body)["errors"][0]["loc"] == ["body", "password"]
