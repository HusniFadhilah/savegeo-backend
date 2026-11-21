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
