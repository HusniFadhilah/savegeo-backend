from __future__ import annotations

import pytest


def test_health_endpoint(client, db_available):
    if not db_available:
        pytest.skip("DATABASE_URL not reachable in this environment")
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "ee_initialized" in body
    assert "status" in body


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_openapi_docs_require_password(client, monkeypatch, path):
    from pydantic import SecretStr
    from app.main import settings

    monkeypatch.setattr(settings, "api_docs_username", "docs-user")
    monkeypatch.setattr(settings, "api_docs_password", SecretStr("a-long-unique-password-for-docs"))
    missing = client.get(path)
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Basic ")
    assert client.get(path, auth=("docs-user", "incorrect")).status_code == 401
    response = client.get(path, auth=("docs-user", "a-long-unique-password-for-docs"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    if path == "/openapi.json":
        assert "paths" in response.json()


def test_openapi_docs_closed_without_configuration(client, monkeypatch):
    from pydantic import SecretStr
    from app.main import settings

    monkeypatch.setattr(settings, "api_docs_username", "")
    monkeypatch.setattr(settings, "api_docs_password", SecretStr(""))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 503


def test_root_landing_available(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
