from __future__ import annotations

import asyncio

import pytest

from app.api.routes import admin
from app.services import ai_provider_service as providers


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    providers.clear_health_cache()
    monkeypatch.setattr(admin.audit_service, "log_audit", lambda *a, **k: None)
    yield
    providers.clear_health_cache()


class _Admin:
    id = 1


def _settings(monkeypatch, values):
    saved = {}
    monkeypatch.setattr(admin.config_service, "get_setting", lambda db, key, default=None: values.get(key, default))
    monkeypatch.setattr(
        admin.config_service, "upsert_setting", lambda db, key, value, updated_by=None: saved.__setitem__(key, value)
    )
    monkeypatch.setattr(admin, "admin_has_permission", lambda a, p: True)
    return saved


def test_config_update_blocks_ollama_model_that_is_not_installed(monkeypatch):
    saved = _settings(monkeypatch, {"ai.ollama_base_url": "http://localhost:11434/v1"})
    monkeypatch.setattr(
        providers, "list_ollama_models",
        lambda base_url, **kw: {"reachable": True, "models": [{"name": "llama3.2:latest"}], "latency_ms": 3, "error": None},
    )
    body = {"updates": [{"key": "ai.provider", "value": "ollama"}, {"key": "ai.model", "value": "qwen3:8b"},
                        {"key": "app.title", "value": "ok"}]}
    result = asyncio.run(admin.config_update(_Request(body), _Admin(), db=None))
    assert saved == {"app.title": "ok"}
    assert {e["key"] for e in result["errors"]} == {"ai.provider", "ai.model"}
    assert "llama3.2:latest" in result["errors"][0]["error"]


def test_config_update_saves_installed_model_and_reports_unreachable_as_warning(monkeypatch):
    saved = _settings(monkeypatch, {"ai.ollama_base_url": "http://localhost:11434/v1"})
    monkeypatch.setattr(
        providers, "list_ollama_models",
        lambda base_url, **kw: {"reachable": False, "models": [], "latency_ms": None, "error": "unreachable"},
    )
    body = {"updates": [{"key": "ai.provider", "value": "ollama"}, {"key": "ai.model", "value": "qwen3:8b"}]}
    result = asyncio.run(admin.config_update(_Request(body), _Admin(), db=None))
    assert saved == {"ai.provider": "ollama", "ai.model": "qwen3:8b"}
    assert result["errors"] == []
    assert len(result["warnings"]) == 1


def test_config_update_rejects_metadata_endpoint_as_ollama_url(monkeypatch):
    saved = _settings(monkeypatch, {})
    body = {"updates": [{"key": "ai.ollama_base_url", "value": "http://169.254.169.254/latest"}]}
    result = asyncio.run(admin.config_update(_Request(body), _Admin(), db=None))
    assert saved == {}
    assert result["errors"][0]["key"] == "ai.ollama_base_url"


def test_ollama_models_route_uses_configured_url_and_reports_real_state(monkeypatch):
    _settings(monkeypatch, {"ai.ollama_base_url": "http://gpu.lan:11434/v1"})
    seen = {}

    def fake_list(base_url, **kw):
        seen["url"] = base_url
        return {"reachable": False, "models": [], "latency_ms": None, "error": "timeout"}

    monkeypatch.setattr(providers, "list_ollama_models", fake_list)
    result = asyncio.run(admin.ollama_models(_Admin(), db=None))
    assert seen["url"] == "http://gpu.lan:11434/v1"
    assert result["reachable"] is False and result["error"] == "timeout"
    assert "gpu.lan" not in repr(result)


def test_ai_health_route_probes_instead_of_assuming_healthy(monkeypatch):
    from app.agentic import agentic_ai

    monkeypatch.setattr(
        agentic_ai, "load_ai_config",
        lambda: {"provider": "ollama", "model": "qwen3:8b", "ollama_base_url": "http://localhost:11434/v1"},
    )
    monkeypatch.setattr(
        providers, "list_ollama_models",
        lambda base_url, **kw: {"reachable": False, "models": [], "latency_ms": None, "error": "unreachable"},
    )
    result = asyncio.run(admin.ai_provider_health(None, None, True, _Admin()))
    assert result["provider"] == "ollama"
    assert result["status"] == "unreachable"
