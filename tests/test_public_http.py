from types import SimpleNamespace

from app.core import public_http as public_http_module

public_http = public_http_module.public_http


def test_public_get_retries_transient_gateway_failure_once(monkeypatch):
    calls = []
    circuit_results = []
    responses = iter([SimpleNamespace(status_code=503), SimpleNamespace(status_code=200)])
    monkeypatch.setattr(public_http_module, "_CIRCUITS", {})
    monkeypatch.setattr(public_http, "_request_once", lambda *_args, **_kwargs: (calls.append(1), next(responses))[1])
    monkeypatch.setattr(public_http, "_circuit_result", lambda host, *, success: circuit_results.append((host, success)))
    monkeypatch.setattr("app.core.public_http.time.sleep", lambda _seconds: None)

    response = public_http.get("https://provider.example/data", timeout=5, max_bytes=1024)

    assert response.status_code == 200
    assert len(calls) == 2
    assert circuit_results == [("provider.example", False), ("provider.example", True)]


def test_public_get_does_not_retry_rate_limit_and_counts_it_as_failure(monkeypatch):
    calls = []
    circuit_results = []
    monkeypatch.setattr(public_http_module, "_CIRCUITS", {})
    monkeypatch.setattr(public_http, "_request_once", lambda *_args, **_kwargs: (calls.append(1), SimpleNamespace(status_code=429))[1])
    monkeypatch.setattr(public_http, "_circuit_result", lambda host, *, success: circuit_results.append((host, success)))

    response = public_http.get("https://provider.example/data", timeout=5, max_bytes=1024)

    assert response.status_code == 429
    assert len(calls) == 1
    assert circuit_results == [("provider.example", False)]


def test_public_post_accepts_bounded_raw_sdk_body_and_provider_headers(monkeypatch):
    captured = {}
    response = SimpleNamespace(status_code=200)
    monkeypatch.setattr(public_http_module, "public_addresses", lambda _host: ["8.8.8.8"])
    monkeypatch.setattr(public_http, "_circuit_before", lambda _host: None)
    monkeypatch.setattr(public_http, "_circuit_result", lambda *_args, **_kwargs: None)

    def request_once(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return response

    monkeypatch.setattr(public_http, "_request_once", request_once)
    result = public_http.request(
        "POST",
        "https://provider.example/v1/chat",
        raw_body=b'{"message":"bounded"}',
        headers={"x-api-key": "test-key", "content-type": "application/json"},
        timeout=5,
        max_bytes=2048,
        max_request_bytes=1024,
    )

    assert result is response
    assert captured["raw_body"] == b'{"message":"bounded"}'
    assert captured["max_request_bytes"] == 1024
    assert captured["headers"] == {"x-api-key": "test-key", "content-type": "application/json"}


def test_public_post_rejects_oversized_raw_sdk_body_before_connect(monkeypatch):
    monkeypatch.setattr(
        public_http_module,
        "public_addresses",
        lambda _host: (_ for _ in ()).throw(AssertionError("oversized body must be rejected before DNS")),
    )

    import pytest

    with pytest.raises(ValueError, match="body exceeds the size limit"):
        public_http.request(
            "POST",
            "https://provider.example/v1/chat",
            raw_body=b"x" * 1025,
            timeout=5,
            max_request_bytes=1024,
        )
