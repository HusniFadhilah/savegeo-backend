from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.agentic import agentic_ai, rate_limiter
from app.db.models.chat_message import ChatMessage
from app.db.models.chat_session import ChatSession
from app.services import agent_service
from app.services import ai_provider_service as providers


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ChatSession.__table__.create(engine)
    ChatMessage.__table__.create(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    providers.clear_health_cache()
    monkeypatch.setattr(rate_limiter, "check", lambda ip: (True, "", 0))
    yield
    providers.clear_health_cache()


def _request() -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": "/api/agent/control", "query_string": b"", "headers": [],
        "client": ("192.0.2.10", 4567), "server": ("savegeo.test", 443),
    })


CFG = {
    "provider": "ollama",
    "model": "qwen3:8b",
    "ollama_base_url": "http://localhost:11434/v1",
    "openrouter_api_key": "sk-or-secret-key-value",
    "fallback_provider": "openrouter",
    "fallback_model": "openai/gpt-4o",
    "allowed_models": [
        {"provider": "ollama", "model": "qwen3:8b"},
        {"provider": "openrouter", "model": "openai/gpt-4o"},
    ],
}


def _control(db, monkeypatch, payload, *, plan, cfg=None, fallback_health="ok"):
    monkeypatch.setattr(agentic_ai, "load_ai_config", lambda: dict(cfg or CFG))
    monkeypatch.setattr(agent_service, "plan_with_ai", plan)
    real_check = providers.check_provider_health

    def fake_check(provider, c, model, **kwargs):
        if provider == "openrouter":
            return {"provider": provider, "model": model, "status": fallback_health}
        return real_check(provider, c, model, **kwargs)

    monkeypatch.setattr(providers, "check_provider_health", fake_check)
    return agent_service.run_agent_control(db, _request(), {"message": "halo", **payload}, owner_key="user:1")


def test_default_selection_is_used_and_reported(db_session, monkeypatch):
    seen = []

    def plan(message, page_state, image, attachment, history):
        seen.append(agentic_ai._get_ai_config()["provider"])
        return {"message": "ok", "actions": []}

    result, status, _ = _control(db_session, monkeypatch, {}, plan=plan)
    assert status == 200
    assert seen == ["ollama"]
    assert result["ai"] == {"provider": "ollama", "model": "qwen3:8b", "fallback_used": False}


def test_user_can_pick_an_allowed_model_for_the_request(db_session, monkeypatch):
    seen = []

    def plan(message, page_state, image, attachment, history):
        cfg = agentic_ai._get_ai_config()
        seen.append((cfg["provider"], cfg["model"]))
        return {"message": "ok", "actions": []}

    result, status, _ = _control(
        db_session, monkeypatch, {"ai": {"provider": "openrouter", "model": "openai/gpt-4o"}}, plan=plan
    )
    assert status == 200
    assert seen == [("openrouter", "openai/gpt-4o")]
    assert result["ai"]["provider"] == "openrouter"


def test_selection_outside_allowlist_is_rejected_before_any_model_call(db_session, monkeypatch):
    def plan(*args, **kwargs):
        raise AssertionError("model must not be called")

    result, status, _ = _control(
        db_session, monkeypatch, {"ai": {"provider": "ollama", "model": "evil:latest"}}, plan=plan
    )
    assert status == 400
    assert "diizinkan" in result["error"]


def test_ollama_outage_falls_back_to_healthy_allowed_provider_and_keeps_session(db_session, monkeypatch):
    calls = []

    def plan(message, page_state, image, attachment, history):
        cfg = agentic_ai._get_ai_config()
        calls.append(cfg["provider"])
        if cfg["provider"] == "ollama":
            raise ConnectionError("Connection error. http://secret-host:11434")
        return {"message": "dari cadangan", "actions": []}

    result, status, _ = _control(db_session, monkeypatch, {}, plan=plan)
    assert status == 200
    assert calls == ["ollama", "openrouter"]
    assert result["message"] == "dari cadangan"
    assert result["ai"] == {
        "provider": "openrouter", "model": "openai/gpt-4o", "fallback_used": True,
        "fallback_from": "ollama", "fallback_reason": "provider_unavailable",
    }
    assert result["session_id"]  # conversation persisted


def test_known_down_ollama_is_skipped_without_a_new_request(db_session, monkeypatch):
    providers.mark_unavailable("ollama", CFG, "qwen3:8b", "unreachable")
    calls = []

    def plan(message, page_state, image, attachment, history):
        calls.append(agentic_ai._get_ai_config()["provider"])
        return {"message": "dari cadangan", "actions": []}

    result, status, _ = _control(db_session, monkeypatch, {}, plan=plan)
    assert status == 200
    assert calls == ["openrouter"]
    assert result["ai"]["fallback_used"] is True


def test_unhealthy_fallback_is_not_used_and_error_is_sanitized(db_session, monkeypatch):
    def plan(message, page_state, image, attachment, history):
        raise ConnectionError("Connection error. http://secret-host:11434 key=sk-or-secret-key-value")

    result, status, _ = _control(db_session, monkeypatch, {}, plan=plan, fallback_health="auth_failed")
    assert status == 500
    assert result["intent"] == "error"
    assert result["ai"]["provider"] == "ollama"
    assert result["ai"]["fallback_used"] is False
    assert result["retryable"] is True
    text = repr(result)
    assert "secret-host" not in text and "sk-or-secret" not in text
    assert "Ollama" in result["message"]


def test_non_availability_errors_do_not_trigger_fallback(db_session, monkeypatch):
    calls = []

    def plan(message, page_state, image, attachment, history):
        calls.append(agentic_ai._get_ai_config()["provider"])
        raise ValueError("API key tidak dikonfigurasi untuk provider ini.")

    result, status, _ = _control(db_session, monkeypatch, {}, plan=plan)
    assert status == 500
    assert calls == ["ollama"]
    assert result["ai"]["fallback_used"] is False


def test_ai_selection_context_is_reset_after_request(db_session, monkeypatch):
    _control(db_session, monkeypatch, {}, plan=lambda *a, **k: {"message": "ok", "actions": []})
    assert agentic_ai._AI_SELECTION.get() is None
