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


def test_openapi_docs_available(client):
    resp = client.get("/docs")
    assert resp.status_code == 200


def test_root_landing_available(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
