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
