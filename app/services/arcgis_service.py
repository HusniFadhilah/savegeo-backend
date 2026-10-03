"""Thin re-export so routes import ArcGIS access through the services layer
(per the project's service/repository/router separation) while the actual
HTTP client implementation stays in app/providers/arcgis_client.py (ported
close to verbatim from the legacy backend).
"""
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.providers.arcgis_client import get_arcgis_client, reset_arcgis_client
from app.services import config_service


def configure_arcgis_from_db(db: Session):
    """Load persisted ArcGIS settings over environment defaults and update the client."""
    settings = get_settings()
    config_service.get_all_settings(db, category="arcgis")
    client = get_arcgis_client()
    client.configure(
        enabled=bool(config_service.get_setting(db, "arcgis.enabled", settings.arcgis_enabled)),
        portal_url=str(config_service.get_setting(db, "arcgis.portal_url", settings.arcgis_portal_url)),
        auth_mode=str(config_service.get_setting(db, "arcgis.auth_mode", settings.arcgis_auth_mode)),
        api_key=str(config_service.get_setting(db, "arcgis.api_key", settings.arcgis_api_key) or ""),
        timeout=int(config_service.get_setting(db, "arcgis.request_timeout", settings.arcgis_request_timeout)),
    )
    return client


__all__ = ["configure_arcgis_from_db", "get_arcgis_client", "reset_arcgis_client"]
