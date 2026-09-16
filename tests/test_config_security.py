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
