from types import ModuleType

import httpx
import pytest
import requests

from app.core import pinned_provider_transport as transport_module


def _response(status: int = 200, body: bytes = b'{"ok":true}'):
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = "application/json"
    response._content = body
    return response


def test_public_sdk_transport_pins_host_and_filters_sdk_headers(monkeypatch):
    calls = {}

    def fake_request(method, url, **kwargs):
        calls.update(method=method, url=url, **kwargs)
        return _response()

    monkeypatch.setattr(transport_module.public_http, "request", fake_request)
    transport = transport_module.BoundedPublicHTTPTransport("api.openai.com")
    request = httpx.Request(
        "POST",
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
            "X-Stainless-Lang": "python",
            "X-Untrusted-Header": "must-not-pass",
        },
        content=b'{"model":"test"}',
    )

    response = transport.handle_request(request)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls["method"] == "POST"
    assert calls["raw_body"] == b'{"model":"test"}'
    assert calls["max_bytes"] == 4 * 1024 * 1024
    assert calls["timeout"] == 30
    assert calls["headers"] == {
        "authorization": "Bearer test-key",
        "content-type": "application/json",
        "x-stainless-lang": "python",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1/chat/completions",
        "https://api.openai.com.evil.example/v1/chat/completions",
        "https://user:secret@api.openai.com/v1/chat/completions",
        "https://api.openai.com:8443/v1/chat/completions",
    ],
)
def test_public_sdk_transport_rejects_destination_changes_without_network(monkeypatch, url):
    monkeypatch.setattr(
        transport_module.public_http,
        "request",
        lambda *_args, **_kwargs: pytest.fail("blocked provider URL must not reach the network"),
    )
    request = httpx.Request("POST", url, content=b"{}")
    with pytest.raises(httpx.RequestError, match="Bounded provider"):
        transport_module.BoundedPublicHTTPTransport("api.openai.com").handle_request(request)


def test_public_sdk_transport_bounds_request_bytes(monkeypatch):
    monkeypatch.setattr(
        transport_module.public_http,
        "request",
        lambda *_args, **_kwargs: pytest.fail("oversized provider request must not reach the network"),
    )
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions", content=b"x" * 2049)
    with pytest.raises(httpx.RequestError, match="Bounded provider"):
        transport_module.BoundedPublicHTTPTransport(
            "api.openai.com", max_request_bytes=2048,
        ).handle_request(request)


def test_real_openai_sdk_uses_pinned_transport_without_network(monkeypatch):
    import json

    from openai import OpenAI

    calls = {}
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_request(method, url, **kwargs):
        calls.update(method=method, url=url, **kwargs)
        return _response(body=json.dumps(payload).encode())

    monkeypatch.setattr(transport_module.public_http, "request", fake_request)
    http_client = transport_module.public_httpx_client("https://api.openai.com/v1")
    client = OpenAI(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        http_client=http_client,
        timeout=5,
        max_retries=0,
    )
    try:
        result = client.chat.completions.create(
            model="gpt-test", messages=[{"role": "user", "content": "hello"}], max_tokens=2,
        )
    finally:
        client.close()

    assert result.choices[0].message.content == "ok"
    assert calls["method"] == "POST"
    assert calls["url"] == "https://api.openai.com/v1/chat/completions"
    assert json.loads(calls["raw_body"])["model"] == "gpt-test"
    assert calls["max_bytes"] == 4 * 1024 * 1024


def test_real_anthropic_sdk_uses_pinned_transport_without_network(monkeypatch):
    import json

    from app.agentic.agentic_ai import _call_anthropic

    calls = {}
    payload = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    def fake_request(method, url, **kwargs):
        calls.update(method=method, url=url, **kwargs)
        return _response(body=json.dumps(payload).encode())

    monkeypatch.setattr(transport_module.public_http, "request", fake_request)
    assert _call_anthropic("test-key", "claude-test", "hello") == "ok"
    assert calls["method"] == "POST"
    assert calls["url"] == "https://api.anthropic.com/v1/messages"
    assert "x-api-key" in calls["headers"]
    assert json.loads(calls["raw_body"])["max_tokens"] == 2048


def test_real_gemini_sdk_uses_pinned_transport_without_network(monkeypatch):
    import json

    from app.agentic.agentic_ai import _call_gemini

    calls = {}
    payload = {
        "candidates": [{
            "content": {"parts": [{"text": "ok"}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
        "modelVersion": "gemini-test",
    }

    def fake_request(method, url, **kwargs):
        calls.update(method=method, url=url, **kwargs)
        return _response(body=json.dumps(payload).encode())

    monkeypatch.setattr(transport_module.public_http, "request", fake_request)
    assert _call_gemini("test-key", "gemini-test", "hello") == "ok"
    assert calls["method"] == "POST"
    assert calls["url"].startswith("https://generativelanguage.googleapis.com/")
    assert calls["url"].endswith("/models/gemini-test:generateContent")
    assert calls["headers"]["x-goog-api-key"] == "test-key"
    assert json.loads(calls["raw_body"])["generationConfig"]["maxOutputTokens"] == 2048


def test_earth_engine_transport_uses_pinned_public_http_and_httplib2_contract(monkeypatch):
    calls = {}

    def fake_request(method, url, **kwargs):
        calls.update(method=method, url=url, **kwargs)
        return _response()

    monkeypatch.setattr(transport_module.public_http, "request", fake_request)
    transport = transport_module.BoundedEarthEngineTransport(timeout=120)
    response, body = transport.request(
        "https://earthengine.googleapis.com/v1/projects/demo/operations",
        method="POST",
        body=b'{"request":true}',
        headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
            "X-Goog-User-Project": "demo-project",
            "X-Internal-Only": "filtered",
        },
    )

    assert response.status == 200
    assert body == b'{"ok":true}'
    assert calls["timeout"] == 60
    assert calls["max_bytes"] == transport.MAX_RESPONSE_BYTES
    assert calls["max_request_bytes"] == transport.MAX_REQUEST_BYTES
    assert calls["headers"] == {
        "authorization": "Bearer secret",
        "content-type": "application/json",
        "x-goog-user-project": "demo-project",
    }


def test_earth_engine_transport_rejects_unlisted_hosts_without_network(monkeypatch):
    monkeypatch.setattr(
        transport_module.public_http,
        "request",
        lambda *_args, **_kwargs: pytest.fail("blocked Earth Engine URL must not reach the network"),
    )
    with pytest.raises(Exception, match="Bounded Earth Engine"):
        transport_module.BoundedEarthEngineTransport().request(
            "https://169.254.169.254/latest/meta-data",
        )


def test_gee_initialization_always_receives_bounded_transport(monkeypatch):
    from app.services import gee_service

    calls = {}
    fake_ee = ModuleType("ee")
    fake_ee.ServiceAccountCredentials = lambda *args, **kwargs: (args, kwargs)
    fake_ee.Initialize = lambda credentials, **kwargs: calls.update(credentials=credentials, kwargs=kwargs)
    monkeypatch.setitem(__import__("sys").modules, "ee", fake_ee)

    assert gee_service._init_from_key_data("svc@example.invalid", b"{}", project_id="demo-project")
    assert isinstance(calls["kwargs"]["http_transport"], transport_module.BoundedEarthEngineTransport)
    assert calls["kwargs"]["project"] == "demo-project"
