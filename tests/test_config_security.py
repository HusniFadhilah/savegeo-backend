from __future__ import annotations

import pytest

from app.core.config import Settings


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
        Settings(app_env="production", jwt_secret_key="x" * 48, **overrides)
