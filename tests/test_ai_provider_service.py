from __future__ import annotations

import pytest

from app.services import ai_provider_service as svc


class FakeHttp:
    """Injectable ``http_get``/``http_post``: maps URL substring -> (status, payload) or an exception."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=5.0):
        self.calls.append(f"{method} {url}")
        for needle, outcome in self.routes.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.fixture(autouse=True)
def _reset_cache():
    svc.clear_health_cache()
    yield
    svc.clear_health_cache()


@pytest.mark.parametrize(
    ("base", "openai_base", "native_root"),
    [
        ("http://localhost:11434/v1", "http://localhost:11434/v1", "http://localhost:11434"),
        ("http://localhost:11434", "http://localhost:11434/v1", "http://localhost:11434"),
        ("http://host.docker.internal:11434/v1/", "http://host.docker.internal:11434/v1", "http://host.docker.internal:11434"),
        ("https://gpu.internal/ollama/v1", "https://gpu.internal/ollama/v1", "https://gpu.internal/ollama"),
    ],
)
def test_normalize_ollama_urls(base, openai_base, native_root):
    assert svc.normalize_ollama_urls(base) == (openai_base, native_root)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://localhost:11434/v1",
        "file:///etc/passwd",
        "http://user:pw@localhost:11434/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]:11434/v1",
        "javascript:alert(1)",
    ],
)
def test_validate_provider_url_rejects_unsafe(url):
    with pytest.raises(ValueError):
        svc.validate_provider_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://localhost:11434/v1", "http://127.0.0.1:11434", "http://host.docker.internal:11434/v1", "https://gpu.example.org/v1"],
)
def test_validate_provider_url_accepts_local_and_public(url):
    assert svc.validate_provider_url(url) == url.rstrip("/")


def test_list_ollama_models_success_sorted_and_sanitized():
    http = FakeHttp(
        {
            "/api/tags": (
                200,
                {
                    "models": [
                        {"name": "qwen3:8b", "size": 5_000_000_000, "details": {"parameter_size": "8B", "family": "qwen3"}},
                        {"name": "llama3.2:latest", "size": 2_000_000_000, "details": {}},
                        {"nope": True},
                    ]
                },
            )
        }
    )
    catalog = svc.list_ollama_models("http://localhost:11434/v1", http=http)
    assert catalog["reachable"] is True
    assert catalog["error"] is None
    assert [m["name"] for m in catalog["models"]] == ["llama3.2:latest", "qwen3:8b"]
    assert catalog["models"][1]["parameter_size"] == "8B"
    assert isinstance(catalog["latency_ms"], int)
    assert http.calls == ["GET http://localhost:11434/api/tags"]


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (TimeoutError("took too long http://secret-host:11434"), "timeout"),
        (ConnectionRefusedError("refused http://secret-host:11434"), "unreachable"),
        ((500, {"error": "boom"}), "http_error"),
        ((200, {"unexpected": []}), "invalid_response"),
        ((200, "not-a-dict"), "invalid_response"),
    ],
)
def test_list_ollama_models_failures_use_codes_not_raw_errors(outcome, code):
    http = FakeHttp({"/api/tags": outcome})
    catalog = svc.list_ollama_models("http://secret-host:11434/v1", http=http)
    assert catalog["reachable"] is False
    assert catalog["models"] == []
    assert catalog["error"] == code
    assert "secret-host" not in repr(catalog)


def test_list_ollama_models_rejects_unsafe_base_url_without_request():
    http = FakeHttp({})
    catalog = svc.list_ollama_models("http://169.254.169.254/", http=http)
    assert catalog["error"] == "invalid_url"
    assert http.calls == []


def test_ollama_health_ok_reports_tool_support():
    http = FakeHttp(
        {
            "/api/tags": (200, {"models": [{"name": "qwen3:8b"}]}),
            "/api/show": (200, {"capabilities": ["completion", "tools"]}),
        }
    )
    health = svc.check_provider_health(
        "ollama", {"ollama_base_url": "http://localhost:11434/v1"}, "qwen3:8b", http=http
    )
    assert health["status"] == "ok"
    assert health["configured"] is True and health["reachable"] is True
    assert health["model_available"] is True
    assert health["tool_calling"] is True


def test_ollama_health_model_missing_lists_alternatives_not_ok():
    http = FakeHttp({"/api/tags": (200, {"models": [{"name": "llama3.2:latest"}]})})
    health = svc.check_provider_health(
        "ollama", {"ollama_base_url": "http://localhost:11434/v1"}, "qwen3:8b", http=http
    )
    assert health["status"] == "model_missing"
    assert health["model_available"] is False
    assert health["available_models"] == ["llama3.2:latest"]


def test_ollama_health_unreachable_is_not_ok_and_cached_to_avoid_hammering():
    http = FakeHttp({"/api/tags": ConnectionRefusedError("nope")})
    cfg = {"ollama_base_url": "http://localhost:11434/v1"}
    first = svc.check_provider_health("ollama", cfg, "qwen3:8b", http=http)
    second = svc.check_provider_health("ollama", cfg, "qwen3:8b", http=http)
    assert first["status"] == "unreachable"
    assert second["status"] == "unreachable"
    assert len(http.calls) == 1  # negative result cached
    third = svc.check_provider_health("ollama", cfg, "qwen3:8b", http=http, force=True)
    assert third["status"] == "unreachable"
    assert len(http.calls) == 2


def test_cloud_provider_without_key_is_unconfigured_and_makes_no_request():
    http = FakeHttp({})
    health = svc.check_provider_health("openrouter", {"openrouter_api_key": ""}, "openai/gpt-4o", http=http)
    assert health["status"] == "unconfigured"
    assert health["configured"] is False
    assert http.calls == []


def test_openrouter_health_auth_failure_and_success_never_leak_key():
    key = "sk-or-v1-supersecretvalue"
    bad = FakeHttp({"openrouter.ai/api/v1/auth/key": (401, {"error": {"message": f"bad {key}"}})})
    health = svc.check_provider_health("openrouter", {"openrouter_api_key": key}, "openai/gpt-4o", http=bad)
    assert health["status"] == "auth_failed"
    assert key not in repr(health)

    svc.clear_health_cache()
    good = FakeHttp({"openrouter.ai/api/v1/auth/key": (200, {"data": {"label": "x"}})})
    health = svc.check_provider_health("openrouter", {"openrouter_api_key": key}, "openai/gpt-4o", http=good)
    assert health["status"] == "ok"
    assert key not in repr(health)


def test_unknown_provider_status():
    assert svc.check_provider_health("nope", {}, "m", http=FakeHttp({}))["status"] == "unknown_provider"


def test_selection_allows_only_configured_allowlist():
    cfg = {
        "provider": "openrouter",
        "model": "openai/gpt-4o",
        "allowed_models": [
            {"provider": "openrouter", "model": "openai/gpt-4o"},
            {"provider": "ollama", "model": "qwen3:8b"},
        ],
    }
    assert svc.resolve_selection(cfg, None) == ("openrouter", "openai/gpt-4o")
    assert svc.resolve_selection(cfg, {"provider": "ollama", "model": "qwen3:8b"}) == ("ollama", "qwen3:8b")
    with pytest.raises(svc.SelectionNotAllowed):
        svc.resolve_selection(cfg, {"provider": "ollama", "model": "evil:latest"})
    with pytest.raises(svc.SelectionNotAllowed):
        svc.resolve_selection(cfg, {"provider": "openai", "model": "gpt-4o"})
    with pytest.raises(svc.SelectionNotAllowed):
        svc.resolve_selection(cfg, {"provider": 5, "model": ["x"]})


def test_selection_without_allowlist_only_permits_the_admin_default():
    cfg = {"provider": "ollama", "model": "qwen3:8b", "allowed_models": []}
    assert svc.resolve_selection(cfg, {"provider": "ollama", "model": "qwen3:8b"}) == ("ollama", "qwen3:8b")
    with pytest.raises(svc.SelectionNotAllowed):
        svc.resolve_selection(cfg, {"provider": "openrouter", "model": "x"})


def test_fallback_candidates_exclude_primary_and_unallowed():
    cfg = {
        "provider": "ollama",
        "model": "qwen3:8b",
        "fallback_provider": "openrouter",
        "fallback_model": "openai/gpt-4o",
        "allowed_models": [
            {"provider": "ollama", "model": "qwen3:8b"},
            {"provider": "openrouter", "model": "openai/gpt-4o"},
        ],
    }
    assert svc.fallback_selection(cfg, ("ollama", "qwen3:8b")) == ("openrouter", "openai/gpt-4o")
    assert svc.fallback_selection(cfg, ("openrouter", "openai/gpt-4o")) is None
    cfg["allowed_models"] = [{"provider": "ollama", "model": "qwen3:8b"}]
    assert svc.fallback_selection(cfg, ("ollama", "qwen3:8b")) is None


def test_is_availability_error_distinguishes_provider_outage_from_bad_request():
    assert svc.is_availability_error(ConnectionError("Connection error."))
    assert svc.is_availability_error(RuntimeError("503 UNAVAILABLE"))
    assert svc.is_availability_error(RuntimeError("429 rate_limit"))
    assert svc.is_availability_error(TimeoutError("timed out"))
    assert not svc.is_availability_error(ValueError("API key tidak dikonfigurasi"))
    assert not svc.is_availability_error(RuntimeError("400 invalid request"))


def _current(values: dict):
    return lambda key: values.get(key, "")


def test_config_update_rejects_unsafe_urls_and_unknown_provider():
    errors, _ = svc.validate_ai_config_update(
        {
            "ai.ollama_base_url": "http://169.254.169.254/latest",
            "ai.custom_base_url": "file:///etc/passwd",
            "ai.provider": "skynet",
            "ai.fallback_provider": "alsonope",
        },
        _current({}),
        http=FakeHttp({}),
    )
    assert set(errors) == {"ai.ollama_base_url", "ai.custom_base_url", "ai.provider", "ai.fallback_provider"}


def test_config_update_validates_allowed_models_json():
    ok, _ = svc.validate_ai_config_update(
        {"ai.allowed_models": '[{"provider":"ollama","model":"qwen3:8b"}]'}, _current({}), http=FakeHttp({})
    )
    assert ok == {}
    for bad in ("not json", '{"a":1}', '[{"provider":"nope","model":"x"}]', '[{"provider":"ollama"}]'):
        errors, _ = svc.validate_ai_config_update({"ai.allowed_models": bad}, _current({}), http=FakeHttp({}))
        assert "ai.allowed_models" in errors, bad


def test_config_update_rejects_ollama_model_that_is_not_installed():
    http = FakeHttp({"/api/tags": (200, {"models": [{"name": "llama3.2:latest"}]})})
    errors, warnings = svc.validate_ai_config_update(
        {"ai.provider": "ollama", "ai.model": "qwen3:8b"},
        _current({"ai.ollama_base_url": "http://localhost:11434/v1"}),
        http=http,
    )
    assert "ai.model" in errors and "ai.provider" in errors
    assert "llama3.2:latest" in errors["ai.model"]
    assert warnings == []


def test_config_update_accepts_installed_ollama_model():
    http = FakeHttp({"/api/tags": (200, {"models": [{"name": "qwen3:8b"}]})})
    errors, warnings = svc.validate_ai_config_update(
        {"ai.provider": "ollama", "ai.model": "qwen3:8b"},
        _current({"ai.ollama_base_url": "http://localhost:11434/v1"}),
        http=http,
    )
    assert errors == {} and warnings == []


def test_config_update_warns_but_allows_when_ollama_unreachable():
    http = FakeHttp({"/api/tags": ConnectionRefusedError("down")})
    errors, warnings = svc.validate_ai_config_update(
        {"ai.provider": "ollama", "ai.model": "qwen3:8b"},
        _current({"ai.ollama_base_url": "http://localhost:11434/v1"}),
        http=http,
    )
    assert errors == {}
    assert len(warnings) == 1 and "tidak dapat dijangkau" in warnings[0]
    assert "localhost" not in warnings[0]


def test_config_update_ignores_unrelated_keys_without_network():
    http = FakeHttp({})
    errors, warnings = svc.validate_ai_config_update({"app.title": "x"}, _current({"ai.provider": "ollama"}), http=http)
    assert errors == {} and warnings == [] and http.calls == []


def test_mark_unavailable_short_circuits_next_health_check_without_network():
    cfg = {"ollama_base_url": "http://localhost:11434/v1"}
    assert svc.peek_cached_health("ollama", cfg, "qwen3:8b") is None
    svc.mark_unavailable("ollama", cfg, "qwen3:8b", "unreachable")
    cached = svc.peek_cached_health("ollama", cfg, "qwen3:8b")
    assert cached is not None and cached["status"] == "unreachable"
    http = FakeHttp({})
    assert svc.check_provider_health("ollama", cfg, "qwen3:8b", http=http)["status"] == "unreachable"
    assert http.calls == []


def test_public_catalog_is_sanitized_and_marks_default_fallback_and_status():
    cfg = {
        "provider": "ollama",
        "model": "qwen3:8b",
        "ollama_base_url": "http://internal-gpu.lan:11434/v1",
        "openrouter_api_key": "sk-or-v1-supersecretvalue",
        "fallback_provider": "openrouter",
        "fallback_model": "openai/gpt-4o",
        "allowed_models": [
            {"provider": "ollama", "model": "qwen3:8b"},
            {"provider": "openrouter", "model": "openai/gpt-4o"},
        ],
    }
    http = FakeHttp(
        {
            "/api/tags": (200, {"models": [{"name": "qwen3:8b"}]}),
            "/api/show": (200, {"capabilities": ["tools"]}),
            "openrouter.ai/api/v1/auth/key": (401, {}),
        }
    )
    catalog = svc.public_catalog(cfg, http=http)
    assert catalog["active"] == {"provider": "ollama", "model": "qwen3:8b"}
    assert catalog["selectable"] is True
    by_id = {p["id"]: p for p in catalog["providers"]}
    assert by_id["ollama:qwen3:8b"]["available"] is True
    assert by_id["ollama:qwen3:8b"]["is_default"] is True
    assert by_id["ollama:qwen3:8b"]["tool_calling"] is True
    assert by_id["openrouter:openai/gpt-4o"]["available"] is False
    assert by_id["openrouter:openai/gpt-4o"]["status"] == "auth_failed"
    assert by_id["openrouter:openai/gpt-4o"]["is_fallback"] is True
    dumped = repr(catalog)
    assert "internal-gpu" not in dumped and "supersecret" not in dumped


def test_public_catalog_not_selectable_without_allowlist():
    cfg = {"provider": "ollama", "model": "qwen3:8b", "ollama_base_url": "http://localhost:11434/v1", "allowed_models": []}
    http = FakeHttp({"/api/tags": (200, {"models": [{"name": "qwen3:8b"}]}), "/api/show": (404, {})})
    catalog = svc.public_catalog(cfg, http=http)
    assert catalog["selectable"] is False
    assert len(catalog["providers"]) == 1
