from __future__ import annotations


def test_request_id_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
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


def test_oversized_dmrv_upload_is_rejected_before_the_body_is_parsed(client):
    path = "/api/dmrv/projects/p-1/field-data/imports"
    headers = {"Content-Type": "multipart/form-data; boundary=x"}
    too_big = client.post(path, content=b"x" * (12 * 1024 * 1024), headers=headers)
    assert too_big.status_code == 413
    body = too_big.json()
    assert body["code"] == "file_too_large" and body["retryable"] is False and body["request_id"]
    assert set(body) == {"code", "message", "request_id", "retryable", "details"}
    assert client.post(path + "/preview", content=b"x" * (12 * 1024 * 1024), headers=headers).status_code == 413
    # ordinary-sized requests are not touched by the size guard (this one fails later, as a malformed form)
    assert client.post(path, content=b"x", headers=headers).status_code != 413
