"""
arcgis_client.py — Minimal ArcGIS REST client.

Reads config from environment variables. Token/API key is never sent to frontend.
All access to ArcGIS REST APIs goes through this module.
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Service URLs allowed for proxy/metadata fetch (prevent open-proxy abuse)
_SERVICE_ALLOWLIST_DOMAINS = {
    "arcgis.com",
    "imagery1.arcgis.com",
    "tiles.arcgis.com",
    "services.arcgis.com",
    "livingatlas.arcgis.com",
    # Carbon datasets hosted outside arcgis.com
    "bd.esri.com",           # geoxc-prod-im.bd.esri.com — WCMC Biomass Carbon Density
    "data-gis.unep-wcmc.org",  # UNEP-WCMC World Biomass Carbon
}


def _is_allowed_domain(url: str) -> bool:
    """Return True if the URL's domain is on the allowlist."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        # strip port if present
        host = host.split(":")[0]
        return any(host == d or host.endswith("." + d) for d in _SERVICE_ALLOWLIST_DOMAINS)
    except Exception:
        return False


class ArcGISClient:
    """
    Minimal ArcGIS REST client backed by environment variables.

    Env vars:
        ARCGIS_ENABLED           — "true" to enable (default: false)
        ARCGIS_PORTAL_URL        — Portal base URL (default: https://www.arcgis.com)
        ARCGIS_AUTH_MODE         — "none" | "api_key" | "oauth2" (default: none)
        ARCGIS_API_KEY           — API key for auth_mode=api_key
        ARCGIS_REQUEST_TIMEOUT   — seconds (default: 30)
    """

    def __init__(self) -> None:
        self._enabled = os.getenv("ARCGIS_ENABLED", "false").lower() == "true"
        self._portal_url = os.getenv("ARCGIS_PORTAL_URL", "https://www.arcgis.com").rstrip("/")
        self._auth_mode = os.getenv("ARCGIS_AUTH_MODE", "none").lower()
        self._api_key = os.getenv("ARCGIS_API_KEY", "")
        self._timeout = int(os.getenv("ARCGIS_REQUEST_TIMEOUT", "30"))

    # ── Public interface ─────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return self._enabled

    def get_status(self) -> Dict[str, Any]:
        """Return public-safe status dict (no token/key values)."""
        status: Dict[str, Any] = {
            "enabled": self._enabled,
            "portal_url": self._portal_url,
            "auth_mode": self._auth_mode,
            "configured": self._is_configured(),
            "checked_at": datetime.utcnow().isoformat() + "Z",
        }

        if not self._enabled:
            status["message"] = "ArcGIS integration is disabled. Set ARCGIS_ENABLED=true to enable."
            return status

        if not self._is_configured():
            status["message"] = "ArcGIS enabled but no credential configured. Set ARCGIS_API_KEY or other auth env vars."
            return status

        # Try a lightweight connectivity check against the portal
        try:
            portal_info = self._fetch_portal_info()
            status["portal_name"] = portal_info.get("portalName") or portal_info.get("name")
            status["portal_version"] = portal_info.get("currentVersion")
            status["can_reach_portal"] = True
        except Exception as exc:
            status["can_reach_portal"] = False
            status["portal_error"] = str(exc)

        return status

    def get_item_metadata(self, item_id: str) -> Dict[str, Any]:
        """Fetch item metadata from ArcGIS portal.

        Raises ValueError if item_id looks malformed.
        Raises requests.RequestException on network errors.
        """
        if not item_id or not item_id.replace("-", "").isalnum():
            raise ValueError(f"Invalid item_id format: {item_id!r}")

        url = f"{self._portal_url}/sharing/rest/content/items/{item_id}"
        params = {"f": "json"}
        params.update(self._get_auth_params())

        resp = requests.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"ArcGIS error: {data['error'].get('message', data['error'])}")

        return {
            "item_id":        data.get("id"),
            "title":          data.get("title"),
            "type":           data.get("type"),
            "description":    data.get("description"),
            "snippet":        data.get("snippet"),
            "tags":           data.get("tags", []),
            "owner":          data.get("owner"),
            "created":        data.get("created"),
            "modified":       data.get("modified"),
            "url":            data.get("url"),
            "extent":         data.get("extent"),
            "spatialReference": data.get("spatialReference"),
            "portal_url":     self._portal_url,
        }

    def get_service_metadata(self, service_url: str) -> Dict[str, Any]:
        """Fetch ArcGIS REST service metadata (ImageServer, MapServer, etc.).

        Only allows URLs on the domain allowlist.
        Raises ValueError for disallowed domains.
        Raises requests.RequestException on network errors.
        """
        if not _is_allowed_domain(service_url):
            raise ValueError(
                f"Service URL domain not allowed: {service_url!r}. "
                f"Allowed domains: {sorted(_SERVICE_ALLOWLIST_DOMAINS)}"
            )

        params = {"f": "json"}
        params.update(self._get_auth_params())

        resp = requests.get(service_url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"ArcGIS service error: {data['error'].get('message', data['error'])}")

        return {
            "service_url":       service_url,
            "name":              data.get("name") or data.get("serviceName"),
            "service_type":      data.get("serviceType") or data.get("type"),
            "current_version":   data.get("currentVersion"),
            "description":       data.get("description"),
            "extent":            data.get("extent") or data.get("initialExtent"),
            "spatial_reference": data.get("spatialReference"),
            "min_scale":         data.get("minScale"),
            "max_scale":         data.get("maxScale"),
            "time_info":         data.get("timeInfo"),
        }

    # ── Private helpers ──────────────────────────────────────────────────

    def _is_configured(self) -> bool:
        if self._auth_mode == "none":
            return True  # public access mode, no cred needed
        if self._auth_mode == "api_key":
            return bool(self._api_key)
        return False

    def _get_auth_params(self) -> Dict[str, str]:
        if self._auth_mode == "api_key" and self._api_key:
            return {"token": self._api_key}
        return {}

    def _fetch_portal_info(self) -> Dict[str, Any]:
        url = f"{self._portal_url}/sharing/rest/portals/self"
        params = {"f": "json"}
        params.update(self._get_auth_params())
        resp = requests.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()


# ── Module-level singleton ───────────────────────────────────────────────

_client_instance: Optional[ArcGISClient] = None


def get_arcgis_client() -> ArcGISClient:
    """Return the module-level ArcGISClient singleton."""
    global _client_instance
    if _client_instance is None:
        _client_instance = ArcGISClient()
    return _client_instance


def reset_arcgis_client() -> None:
    """Force re-creation of the singleton (e.g., after env var change in tests)."""
    global _client_instance
    _client_instance = None
