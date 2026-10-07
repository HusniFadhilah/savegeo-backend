from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models.imagery_provider_entry import ImageryProviderEntry
from app.db.models.system_config import SystemConfig
from app.repositories import imagery_provider_repo
from app.services import config_service
from app.services import imagery_provider_config
from app.services.commercial_imagery_service import configuration as commercial_configuration


@pytest.fixture()
def db():
    config_service.invalidate()
    engine = create_engine("sqlite:///:memory:")
    ImageryProviderEntry.__table__.create(engine)
    SystemConfig.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
    config_service.invalidate()


def test_imagery_provider_catalog_overlay_updates_metadata_and_activation(db):
    imagery_provider_repo.refresh_cache(db)
    before = {row["key"]: row for row in imagery_provider_repo.list_provider_rows(db)}
    assert "sentinel2" in before
    assert "airbus_oneatlas" in before

    imagery_provider_repo.update_provider(
        db,
        "sentinel2",
        {"name": "Sentinel-2 admin", "is_active": False, "display_order": 4},
    )
    after = {row["key"]: row for row in imagery_provider_repo.list_provider_rows(db)}
    assert after["sentinel2"]["name"] == "Sentinel-2 admin"
    assert after["sentinel2"]["display_order"] == 4
    assert imagery_provider_repo.provider_is_active("sentinel2") is False

    assert imagery_provider_repo.reset_provider(db, "sentinel2") is True
    assert imagery_provider_repo.provider_is_active("sentinel2") is True


def test_commercial_connection_is_applied_without_exposing_credential(db, monkeypatch):
    monkeypatch.setattr(imagery_provider_config, "get_settings", lambda: Settings(app_env="test"))
    secret = "planet-test-secret-value"
    imagery_provider_config.save_connection(
        db,
        "planet_commercial",
        {
            "fields": {
                "catalog_url": "https://catalog.example/stac",
                "collection": "basemaps",
                "auth_scheme": "bearer",
            },
            "credential": secret,
        },
        updated_by=7,
    )

    settings = imagery_provider_config.effective_settings(db)
    assert settings.planet_stac_url == "https://catalog.example/stac"
    assert settings.planet_stac_collection == "basemaps"
    assert settings.planet_api_key == secret
    view = imagery_provider_config.admin_connection_view("planet_commercial", settings)
    assert view["credential_configured"] is True
    assert secret not in repr(view)

    imagery_provider_config.save_connection(db, "planet_commercial", {"clear_credential": True}, updated_by=7)
    assert imagery_provider_config.effective_settings(db).planet_api_key == ""


def test_public_stac_connection_can_use_explicit_no_auth_scheme():
    settings = Settings(
        app_env="test",
        planet_stac_url="https://catalog.example/stac",
        planet_stac_collection="public-collection",
        planet_auth_scheme="none",
    )
    assert commercial_configuration("planet_commercial", settings)["configured"] is True
