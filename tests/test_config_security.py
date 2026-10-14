from __future__ import annotations

import pytest
import secrets

from app.core.config import Settings

TEST_SECRET = secrets.token_urlsafe(64)


def test_production_requires_stable_jwt_secret():
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(app_env="production", jwt_secret_key="")
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(app_env="production", jwt_secret_key="too-short")


def test_development_can_generate_ephemeral_jwt_secret():
    settings = Settings(app_env="development", jwt_secret_key="")
    assert len(settings.jwt_secret_key) >= 32


@pytest.mark.parametrize("overrides", [
    {"auth_cookie_secure": False}, {"csrf_protection_enabled": False}, {"allowed_origins": "*"},
])
def test_production_cannot_disable_browser_session_protections(overrides):
    with pytest.raises(ValueError):
        Settings(app_env="production", jwt_secret_key=TEST_SECRET, **overrides)


@pytest.mark.parametrize("key", ["a" * 96, "0123456789abcdef" * 4, "eyJ" + TEST_SECRET, TEST_SECRET[:32]])
def test_production_rejects_predictable_and_public_token_signing_keys(key):
    with pytest.raises(ValueError, match="JWT_SECRET_KEY") as error:
        Settings(_env_file=None, app_env="production", jwt_secret_key=key)
    assert key not in str(error.value)


def test_production_rejects_credential_reuse():
    with pytest.raises(ValueError, match="must not reuse"):
        Settings(_env_file=None, app_env="production", jwt_secret_key=TEST_SECRET, cams_ads_api_key=TEST_SECRET)


def test_production_accepts_independent_random_signing_key():
    assert Settings(_env_file=None, app_env="production", jwt_secret_key=TEST_SECRET).jwt_secret_key == TEST_SECRET
