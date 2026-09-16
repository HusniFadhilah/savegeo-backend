from __future__ import annotations


def test_request_id_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_valid_request_id_is_echoed(client):
    request_id = "release-test.2026-09-16"
    response = client.get("/", headers={"X-Request-ID": request_id})
    assert response.headers["X-Request-ID"] == request_id


def test_http_errors_use_problem_details_and_do_not_leak_exception(client):
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["request_id"]
    assert body["error"] == body["detail"]
