"""Apple MapKit JS token endpoint for the Apple satellite basemap."""
from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import maps
from app.core.config import get_settings
from app.core.security import get_current_app_viewer
from app.db.models.user import User


@pytest.fixture()
def signing_key():
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, private.public_key()


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(maps.router, prefix="/api")
    app.dependency_overrides[get_current_app_viewer] = lambda: User(id=1, username="alice")
    with TestClient(app) as test_client:
        yield test_client


def _configure(monkeypatch, pem: str, **extra):
    settings = get_settings()
    monkeypatch.setattr(settings, "apple_mapkit_team_id", "TEAM123456")
    monkeypatch.setattr(settings, "apple_mapkit_key_id", "KEY1234567")
    # Environment files usually carry the PEM on one line with escaped newlines.
    monkeypatch.setattr(settings, "apple_mapkit_private_key", pem.replace("\n", "\\n"))
    monkeypatch.setattr(settings, "apple_mapkit_private_key_file", "")
    for name, value in extra.items():
        monkeypatch.setattr(settings, name, value)
    return settings


def test_token_is_es256_short_lived_and_bound_to_allowed_origin(client, monkeypatch, signing_key):
    pem, public_key = signing_key
    settings = _configure(monkeypatch, pem, allowed_origins="https://savegeo.example,https://begeo.example")
    response = client.get("/api/maps/apple-mapkit/token", headers={"Referer": "https://begeo.example/app?x=1"})
    assert response.status_code == 200
    body = response.json()
    header = jwt.get_unverified_header(body["token"])
    assert header == {"alg": "ES256", "kid": "KEY1234567", "typ": "JWT"}
    claims = jwt.decode(body["token"], public_key, algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"
    assert claims["origin"] == "https://begeo.example" == body["origin"]
    assert 0 < claims["exp"] - claims["iat"] <= settings.apple_mapkit_token_ttl_seconds
    assert "PRIVATE KEY" not in response.text


def test_foreign_origin_falls_back_to_frontend_base_url(client, monkeypatch, signing_key):
    pem, public_key = signing_key
    _configure(monkeypatch, pem, allowed_origins="https://savegeo.example", frontend_base_url="https://savegeo.example/")
    response = client.get("/api/maps/apple-mapkit/token", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert jwt.decode(response.json()["token"], public_key, algorithms=["ES256"])["origin"] == "https://savegeo.example"


def test_unconfigured_or_broken_key_returns_503_without_details(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "apple_mapkit_team_id", "")
    assert client.get("/api/maps/apple-mapkit/token").status_code == 503
    _configure(monkeypatch, "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----")
    broken = client.get("/api/maps/apple-mapkit/token")
    assert broken.status_code == 503
    assert "not-a-key" not in broken.text


def test_token_requires_signed_in_viewer():
    app = FastAPI()
    app.include_router(maps.router, prefix="/api")
    from app.db.session import get_db

    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as test_client:
        assert test_client.get("/api/maps/apple-mapkit/token").status_code == 401
