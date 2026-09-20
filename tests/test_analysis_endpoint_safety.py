"""Guard costly jobs and keep internal exceptions out of API responses."""

from types import SimpleNamespace
from threading import BoundedSemaphore
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.routes import analysis_jobs
from app.api.routes import carbon, landcover, regions
from app.api.deps import require_ee
from app.main import app
from app.core.security import get_current_app_viewer
from app.db.session import get_db
from app.services import carbon_service
from app.registries.carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY
from app.services import crop_monitoring_service, landcover_service, vegetation_service
from app.services import samgeo_service
from app.services import gee_common
from app.services.gee_common import AnalysisError


def test_samgeo_jobs_require_login():
    with TestClient(app) as client:
        create = client.post("/api/imagery/samgeo/jobs", json={})
        read = client.get("/api/imagery/samgeo/jobs/00000000-0000-0000-0000-000000000000")
    assert create.status_code == 401
    assert read.status_code == 401


def test_crop_monitoring_analysis_requires_login():
    app.dependency_overrides[require_ee] = lambda: None
    app.dependency_overrides[get_db] = lambda: object()
    try:
        with TestClient(app) as client:
            response = client.post("/api/analyze/crop-monitoring", json={})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "service", "method"),
    [
        ("/api/analyze/landcover", landcover_service, "analyze_landcover"),
        ("/api/analyze/vegetation", vegetation_service, "analyze_vegetation"),
        ("/api/analyze/crop-monitoring", crop_monitoring_service, "run_crop_monitoring"),
    ],
)
def test_analysis_routes_hide_internal_errors(monkeypatch, path, service, method):
    def fail(*_args):
        raise RuntimeError("private-upstream-token")

    monkeypatch.setattr(service, method, fail)
    app.dependency_overrides[require_ee] = lambda: None
    app.dependency_overrides[get_current_app_viewer] = lambda: object()
    app.dependency_overrides[get_db] = lambda: object()
    try:
        with TestClient(app) as client:
            response = client.post(path, json={})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert "private-upstream-token" not in response.text


def test_region_provider_errors_do_not_expose_upstream_details(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("private-region-provider-url")

    monkeypatch.setattr(regions.requests, "get", fail)
    with pytest.raises(HTTPException) as error:
        regions.get_provinces()
    assert error.value.status_code == 502
    assert "private-region-provider-url" not in str(error.value.detail)


@pytest.mark.parametrize(
    "bounds",
    [
        {"west": 110, "south": -7, "east": 100, "north": -6},
        {"west": 110, "south": -7, "east": 111, "north": float("nan")},
        {"west": 181, "south": -7, "east": 182, "north": -6},
    ],
)
def test_invalid_aoi_bounds_rejected_before_earth_engine(bounds):
    with pytest.raises(ValueError):
        gee_common.create_geometry_from_payload(bounds)


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [181, 0]},
        {"type": "Polygon", "coordinates": [[[110, -7], [111, -7], [111, -6]]]},
        {"type": "FeatureCollection", "features": []},
        {"type": "Point", "coordinates": [10**1000, 0]},
    ],
)
def test_invalid_geojson_rejected_before_earth_engine(geometry):
    with pytest.raises(ValueError):
        gee_common.geojson_to_ee_geometry(geometry)


def test_valid_geojson_feature_passes_coordinate_validation():
    gee_common._validate_geojson(
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[110, -7], [111, -7], [111, -6], [110, -7]],
                ],
            },
        }
    )


def test_landcover_invalid_aoi_returns_client_error(monkeypatch):
    def invalid(_data):
        raise ValueError("private-coordinate-context")

    async def request_json():
        return {"aoi": {}}

    monkeypatch.setattr(landcover.landcover_service, "analyze_landcover", invalid)
    with pytest.raises(HTTPException) as error:
        asyncio.run(landcover.analyze_landcover(SimpleNamespace(json=request_json)))
    assert error.value.status_code == 400
    assert "private-coordinate-context" not in str(error.value.detail)


def test_upstream_analysis_error_keeps_status_and_hides_details(monkeypatch):
    def unavailable(_data):
        raise AnalysisError("https://provider.example/?token=private-token", 502)

    async def request_json():
        return {}

    monkeypatch.setattr(landcover.landcover_service, "analyze_landcover", unavailable)
    with pytest.raises(HTTPException) as error:
        asyncio.run(landcover.analyze_landcover(SimpleNamespace(json=request_json)))
    assert error.value.status_code == 502
    assert "private-token" not in str(error.value.detail)


def test_forced_dataset_health_refresh_requires_admin():
    with TestClient(app) as client:
        response = client.post("/api/carbon/datasets/health/refresh")
    assert response.status_code == 401


def test_dataset_health_hides_upstream_url_and_error(monkeypatch):
    import requests

    def fail(*_args, **_kwargs):
        raise requests.RequestException("https://provider.example/?token=private-token")

    monkeypatch.setattr(carbon_service.requests, "head", fail)
    key = next(iter(CARBON_EXTERNAL_REGISTRY))
    carbon_service._DATASET_HEALTH_CACHE.pop(key, None)
    result = carbon_service.check_external_dataset_health(key, refresh=True)
    assert result["status"] == "unavailable"
    assert "url" not in result
    assert "private-token" not in str(result)


def test_public_dataset_health_uses_cache_without_network(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("Public status endpoint must not probe upstream")

    monkeypatch.setattr(carbon_service.requests, "head", fail)
    carbon_service._DATASET_HEALTH_CACHE.clear()
    result = carbon.carbon_dataset_health()
    assert result["datasets"]
    assert all(item["status"] == "unknown" for item in result["datasets"])


def test_dataset_health_closes_probe_responses(monkeypatch):
    closed = []
    head_response = SimpleNamespace(status_code=405, close=lambda: closed.append("HEAD"))
    get_response = SimpleNamespace(status_code=206, close=lambda: closed.append("GET"))
    monkeypatch.setattr(carbon_service.requests, "head", lambda *_a, **_kw: head_response)
    monkeypatch.setattr(carbon_service.requests, "get", lambda *_a, **_kw: get_response)
    key = next(iter(CARBON_EXTERNAL_REGISTRY))
    result = carbon_service.check_external_dataset_health(key, refresh=True)
    assert result["status"] == "healthy"
    assert closed == ["HEAD", "GET"]


def test_expired_health_cache_does_not_claim_provider_is_healthy():
    key = next(iter(CARBON_EXTERNAL_REGISTRY))
    carbon_service._DATASET_HEALTH_CACHE[key] = (0, {"key": key, "status": "healthy"})
    result = carbon_service.get_cached_external_dataset_health(key)
    assert result["status"] == "unknown"
    assert result["stale"] is True


def test_analysis_job_queue_rejects_overload(monkeypatch):
    slots = BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(analysis_jobs, "_job_slots", slots)

    with pytest.raises(HTTPException) as error:
        analysis_jobs.create_analysis_job(
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ee_initialized=False))),
            data={"type": "carbon_local", "payload": {}},
            viewer=SimpleNamespace(id=1),
            db=object(),
        )
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "30"


def test_analysis_job_slot_released_after_invalid_provenance(monkeypatch):
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(analysis_jobs, "_job_slots", slots)

    def invalid(*_args, **_kwargs):
        raise ValueError("invalid date")

    db = SimpleNamespace(rollback=lambda: None)
    monkeypatch.setattr(analysis_jobs.provenance_service, "create_analysis_provenance", invalid)
    with pytest.raises(HTTPException) as error:
        analysis_jobs.create_analysis_job(
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ee_initialized=False))),
            data={"type": "carbon_local", "payload": {}},
            viewer=SimpleNamespace(id=1),
            db=db,
        )
    assert error.value.status_code == 422
    assert slots.acquire(blocking=False)


def test_carbon_local_internal_error_does_not_expose_exception(monkeypatch):
    def fail(_db, _data):
        raise RuntimeError("private-upstream-token")

    monkeypatch.setattr(carbon_service, "analyze_carbon_local", fail)
    app.dependency_overrides[get_current_app_viewer] = lambda: object()
    app.dependency_overrides[get_db] = lambda: object()
    try:
        with TestClient(app) as client:
            response = client.post("/api/analyze/carbon-local", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert "private-upstream-token" not in response.text


def test_analysis_job_result_is_visible_only_to_its_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis_jobs, "_jobs_dir", lambda: tmp_path)
    job_id = "00000000-0000-0000-0000-000000000001"
    analysis_jobs._write_job(job_id, {"status": "queued", "owner": "admin:1"})
    analysis_jobs._write_job(job_id, {"status": "succeeded", "result": {"value": 7}})

    own_result = analysis_jobs.get_analysis_job(job_id, viewer=SimpleNamespace(id=1))
    assert own_result["result"] == {"value": 7}
    assert "owner" not in own_result
    with pytest.raises(HTTPException) as denied:
        analysis_jobs.get_analysis_job(job_id, viewer=SimpleNamespace(id=2))
    assert denied.value.status_code == 404


def test_samgeo_status_keeps_owner_and_hides_it_from_response(monkeypatch, tmp_path):
    monkeypatch.setattr(samgeo_service, "JOB_ROOT", tmp_path)
    job_id = "00000000-0000-0000-0000-000000000002"
    folder = tmp_path / job_id
    folder.mkdir()
    status_path = folder / "status.json"
    samgeo_service._write_json(status_path, {"status": "running", "owner": "admin:1"})
    samgeo_service._write_json(status_path, {"status": "complete", "result": {"features": []}})

    own_result = samgeo_service.get_job(job_id, owner="admin:1")
    assert own_result["status"] == "complete"
    assert "owner" not in own_result
    with pytest.raises(AnalysisError) as denied:
        samgeo_service.get_job(job_id, owner="admin:2")
    assert denied.value.status_code == 404
