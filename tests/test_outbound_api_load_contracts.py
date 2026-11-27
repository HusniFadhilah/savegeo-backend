"""Regression checks for egress policy, typed request bodies, errors and load admission."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas.admin import (
    ArcGISConfigUpdateRequest,
    CompanyBoundaryCreateRequest,
    CompanyOSMImportRequest,
    ConfigBulkUpdateRequest,
    ImageryProviderConnectionRequest,
    ImageryProviderUpdateRequest,
)
from app.services.dmrv.errors import DmrvError, register_dmrv_error_handler


BACKEND = Path(__file__).resolve().parents[1]


def test_direct_requests_calls_are_restricted_to_configured_local_ai_probes():
    network_call = re.compile(r"\brequests\.(?:get|post|put|patch|delete|request)\s*\(")
    offenders = []
    for source in (BACKEND / "app").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        if not network_call.search(text):
            continue
        relative = source.relative_to(BACKEND).as_posix()
        if relative == "app/services/ai_provider_service.py" or "from app.core.public_http import public_http as requests" in text:
            continue
        else:
            offenders.append(source.relative_to(BACKEND).as_posix())
    assert offenders == [], f"Network calls must use the bounded shared transport: {offenders}"


def test_known_admin_write_payloads_have_typed_bounded_models():
    assert ArcGISConfigUpdateRequest.model_validate({
        "enabled": True, "portal_url": "https://www.arcgis.com", "auth_mode": "none",
    }).request_timeout == 30
    assert CompanyBoundaryCreateRequest.model_validate({
        "name": "Area", "industry_type": "forestry", "geojson": {"type": "Feature"},
    }).name == "Area"
    assert CompanyOSMImportRequest.model_validate({"industry_types": ["forestry"]}).industry_types == ["forestry"]
    assert ConfigBulkUpdateRequest.model_validate({"updates": [{"key": "app.title", "value": "SaveGeo"}]})
    assert ImageryProviderUpdateRequest.model_validate({"is_active": False}).is_active is False
    assert ImageryProviderConnectionRequest.model_validate({"fields": {"collection": "test"}}).fields["collection"] == "test"

    for schema, payload in (
        (ArcGISConfigUpdateRequest, {"enabled": True, "portal_url": "https://x", "auth_mode": "none", "unexpected": True}),
        (CompanyBoundaryCreateRequest, {"name": "Area", "industry_type": "forest", "extra": "value"}),
        (CompanyOSMImportRequest, {"industry_types": [1]}),
        (ConfigBulkUpdateRequest, {"updates": [{"key": "", "value": 1}]}),
        (ImageryProviderUpdateRequest, {"is_active": None}),
        (ImageryProviderConnectionRequest, {"credential": "secret", "clear_credential": True}),
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(payload)


def test_route_json_parsing_does_not_bypass_typed_validation():
    bypasses = []
    for source in (BACKEND / "app/api/routes").glob("*.py"):
        text = source.read_text(encoding="utf-8")
        if "read_bounded_json_object(request)" in text:
            bypasses.append(source.name)
        if "request.json()" in text:
            bypasses.append(source.name)
    # The shapefile converter has a separate streaming body-size check and
    # validates its parsed object with ShapefileConversionRequest.
    assert bypasses == []
    utils = (BACKEND / "app/api/routes/utils.py").read_text(encoding="utf-8")
    assert "validate_typed_json_object(data, ShapefileConversionRequest)" in utils


def test_dmrv_errors_use_the_shared_problem_details_contract():
    app = FastAPI()
    register_dmrv_error_handler(app)

    @app.get("/sample")
    def sample():
        raise DmrvError(503, "provider_unavailable", "Layanan sedang tidak tersedia.", retryable=True)

    response = TestClient(app).get("/sample")
    body = response.json()
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["type"] == "urn:savegeo:problem:503"
    assert body["status"] == 503
    assert body["code"] == "provider_unavailable"
    assert body["detail"] == body["error"] == body["message"]
    assert body["instance"] == "/sample"
    assert body["retryable"] is True


@pytest.mark.asyncio
async def test_agent_rate_limit_uses_problem_details_and_keeps_ui_extensions(monkeypatch):
    from app.api.routes import agent
    from app.main import _problem_response
    from fastapi import HTTPException
    from starlette.requests import Request

    async def fake_payload(*_args, **_kwargs):
        return {"message": "status check"}

    monkeypatch.setattr(agent, "read_typed_json_object", fake_payload)
    monkeypatch.setattr(agent, "chat_session_owner_key", lambda _viewer: "owner-1")
    monkeypatch.setattr(agent, "viewer_feature_codes", lambda *_args: set())
    monkeypatch.setattr(
        agent.agent_service, "run_agent_control",
        lambda *_args, **_kwargs: ({
            "intent": "rate_limited", "confidence": 0, "needs_confirmation": False,
            "message": "Batas chatbot tercapai.", "warnings": [], "actions": [],
            "retry_after": 12,
        }, 429, {"Retry-After": "12"}),
    )
    scope = {"type": "http", "method": "POST", "path": "/api/agent/control", "headers": [], "state": {}}
    request = Request(scope)

    with pytest.raises(HTTPException) as error:
        await agent.agent_control(request, viewer=object(), db=object())

    assert error.value.status_code == 429
    response = _problem_response(request, error.value.status_code, error.value.detail, headers=error.value.headers)
    body = response.body.decode("utf-8")
    assert response.media_type == "application/problem+json"
    assert '"code":"agent_rate_limited"' in body
    assert '"status":429' in body
    assert '"message":"Batas chatbot tercapai."' in body
    assert '"intent":"rate_limited"' in body
    assert response.headers["Retry-After"] == "12"


def test_registry_connector_uses_the_pinned_bounded_transport(monkeypatch):
    from app.services.dmrv import registry_connector

    endpoint = "https://registry.example/api/read"
    calls = []
    monkeypatch.setattr(
        registry_connector, "_settings",
        lambda: (endpoint, "read-only-secret", "verra_gateway", {"registry.example"}),
    )
    monkeypatch.setattr(registry_connector, "status", lambda: {"configured": True})
    monkeypatch.setattr(
        registry_connector.public_http, "validate_public_destination",
        lambda url: calls.append(("validate", url)),
    )
    monkeypatch.setattr(
        registry_connector.public_http, "get",
        lambda url, **kwargs: (
            calls.append(("get", url, kwargs))
            or SimpleNamespace(
                is_redirect=False,
                status_code=200,
                json=lambda: {
                    "source_url": endpoint,
                    "rows": [{
                        "external_project_id": "project-1", "vintage": 2024, "serial": "SER-1",
                        "event_type": "issuance", "event_date": "2025-01-01", "quantity": 1,
                        "account": "account-1", "transaction_id": "TX-1",
                    }],
                },
            )
        ),
    )

    result = registry_connector.fetch("Verra Registry", "project-1")

    assert result.rows[0].transaction_id == "TX-1"
    assert calls[0] == ("validate", endpoint)
    assert calls[1][0:2] == ("get", endpoint)
    assert calls[1][2]["max_bytes"] == registry_connector.MAX_RESPONSE_BYTES
    assert calls[1][2]["headers"]["Authorization"] == "Bearer read-only-secret"


def test_analysis_job_admission_stays_bounded_during_concurrent_requests(monkeypatch, tmp_path):
    from app.api.routes import analysis_jobs
    from app.db.models.admin_user import AdminUser

    limit = 3
    slots = BoundedSemaphore(limit)
    release_jobs = Event()
    started_jobs = Event()
    worker_pool = ThreadPoolExecutor(max_workers=limit)
    accepted = 0
    from threading import Lock

    counter_lock = Lock()

    def hold_job(*_args):
        started_jobs.set()
        release_jobs.wait(timeout=10)

    monkeypatch.setattr(analysis_jobs, "_job_slots", slots)
    monkeypatch.setattr(analysis_jobs, "_executor", worker_pool)
    monkeypatch.setattr(analysis_jobs, "_jobs_dir", lambda: tmp_path)
    monkeypatch.setattr(analysis_jobs, "_write_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(analysis_jobs, "_run_job", hold_job)
    monkeypatch.setattr(analysis_jobs.analysis_queue, "enabled", lambda: False)
    monkeypatch.setattr(analysis_jobs, "_carbon_cache_key", lambda *_args: None)
    monkeypatch.setattr(analysis_jobs, "_model_pin_for_job", lambda *_args: None)
    monkeypatch.setattr(analysis_jobs, "requires_earth_engine", lambda *_args: False)
    monkeypatch.setattr(analysis_jobs, "run_analysis_preflight", lambda *_args: {"status": "ready"})
    monkeypatch.setattr(analysis_jobs, "ensure_job_feature_access", lambda *_args: None)
    monkeypatch.setattr(analysis_jobs.provenance_service, "create_analysis_provenance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(analysis_jobs.output_registry_service, "register_action_outputs", lambda *_args: None)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ee_initialized=False)), headers={})
    viewer = AdminUser(id=55, username="load-test", password_hash="", role_id=None, is_superuser=True)

    def submit(_index):
        nonlocal accepted
        try:
            result = analysis_jobs._create_analysis_job_sync(
                request=request, data={"type": "carbon_local", "payload": {}}, viewer=viewer, db=object(),
            )
            with counter_lock:
                accepted += 1
            return result["status"]
        except HTTPException as exc:
            assert exc.status_code == 429
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=24) as clients:
            results = list(clients.map(submit, range(24)))
        assert started_jobs.wait(timeout=2)
        assert accepted == limit
        assert results.count("rejected") == 24 - limit
        assert not slots.acquire(blocking=False)
    finally:
        release_jobs.set()
        worker_pool.shutdown(wait=True, cancel_futures=True)
    assert all(slots.acquire(blocking=False) for _ in range(limit))
