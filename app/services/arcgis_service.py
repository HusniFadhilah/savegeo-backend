"""Thin re-export so routes import ArcGIS access through the services layer
(per the project's service/repository/router separation) while the actual
HTTP client implementation stays in app/providers/arcgis_client.py (ported
close to verbatim from the legacy backend).
"""
from app.providers.arcgis_client import get_arcgis_client, reset_arcgis_client

__all__ = ["get_arcgis_client", "reset_arcgis_client"]
