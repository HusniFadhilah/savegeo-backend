"""Thin re-export so routes import ArcGIS access through the services layer
(per the project's service/repository/router separation) while the actual
HTTP client implementation stays in app/providers/arcgis_client.py (ported
close to verbatim from the legacy backend).
"""
from sqlalchemy.orm import Session
import threading
import time

from app.core.config import get_settings
from app.providers.arcgis_client import get_arcgis_client, reset_arcgis_client
from app.services import config_service

_ARC_GIS_CONFIG_TTL_SECONDS = 30.0
_arcgis_config_lock = threading.Lock()
_arcgis_configured_at = 0.0


def configure_arcgis_from_db(db: Session, *, force: bool = False):
    """Load persisted ArcGIS settings into this worker's client when stale.

    Admin settings are stored in the shared database, while the ArcGIS client
    is process-local. Refreshing on a short TTL lets long-lived API workers pick
    up updates made by another worker without querying configuration for every
    map tile. Writers can force an immediate refresh in their own process.
    """
    global _arcgis_configured_at
    client = get_arcgis_client()
    now = time.monotonic()
    if not force and now - _arcgis_configured_at < _ARC_GIS_CONFIG_TTL_SECONDS:
        return client

    with _arcgis_config_lock:
        now = time.monotonic()
        if not force and now - _arcgis_configured_at < _ARC_GIS_CONFIG_TTL_SECONDS:
            return client
        _configure_arcgis_from_db(db, client)
        _arcgis_configured_at = time.monotonic()
    return client


def _configure_arcgis_from_db(db: Session, client) -> None:
    settings = get_settings()
    config_service.get_all_settings(db, category="arcgis")
    client.configure(
        enabled=bool(config_service.get_setting(db, "arcgis.enabled", settings.arcgis_enabled)),
        portal_url=str(config_service.get_setting(db, "arcgis.portal_url", settings.arcgis_portal_url)),
        auth_mode=str(config_service.get_setting(db, "arcgis.auth_mode", settings.arcgis_auth_mode)),
        api_key=str(config_service.get_setting(db, "arcgis.api_key", settings.arcgis_api_key) or ""),
        timeout=int(config_service.get_setting(db, "arcgis.request_timeout", settings.arcgis_request_timeout)),
    )


def reset_arcgis_config_cache() -> None:
    """Clear process-local client state for tests and explicit reconfiguration."""
    global _arcgis_configured_at
    with _arcgis_config_lock:
        _arcgis_configured_at = 0.0
        reset_arcgis_client()


__all__ = ["configure_arcgis_from_db", "get_arcgis_client", "reset_arcgis_client", "reset_arcgis_config_cache"]
