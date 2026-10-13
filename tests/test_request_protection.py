from __future__ import annotations


def test_cross_origin_cookie_mutation_is_rejected(client):
    response = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://attacker.example"},
        cookies={"savegeo_user_session": "placeholder"},
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["request_id"]


def test_cookie_mutation_without_origin_or_referer_is_rejected(client):
    client.headers.pop("Origin", None)
    response = client.post("/api/auth/logout", cookies={"savegeo_user_session": "placeholder"})
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_login_csrf_is_rejected_even_without_session_cookie(client):
    response = client.post("/api/auth/login", headers={"Origin": "https://attacker.example"},
                           json={"username": "attacker", "password": "password123"})
    assert response.status_code == 403
