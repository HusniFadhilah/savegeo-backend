"""Configuration and credential boundary for licensed imagery providers.

The imagery service deliberately talks to providers through a small, generic
STAC adapter.  This keeps Planet, Vantor/Maxar, and ICEYE credentials on the
server while allowing each provider to expose its own catalog URL and
collection.  Provider-specific ordering/tasking APIs are intentionally not
called by scene search or preview.
"""
from __future__ import annotations

import base64
from typing import Any

from app.core.config import Settings, get_settings

COMMERCIAL_PROVIDER_KEYS = {
    "planet_commercial", "vantor_commercial", "iceye_commercial",
    "airbus_oneatlas", "blacksky_spectra",
}

_CONFIG = {
    "planet_commercial": {
        "label": "Planet commercial",
        "url": "planet_stac_url",
        "collection": "planet_stac_collection",
        "credential": "planet_api_key",
        "scheme": "planet_auth_scheme",
    },
    "vantor_commercial": {
        "label": "Vantor/Maxar commercial",
        "url": "vantor_stac_url",
        "collection": "vantor_stac_collection",
        "credential": "vantor_api_token",
        "scheme": "vantor_auth_scheme",
    },
    "iceye_commercial": {
        "label": "ICEYE commercial SAR",
        "url": "iceye_stac_url",
        "collection": "iceye_stac_collection",
        "credential": "iceye_api_token",
        "scheme": "iceye_auth_scheme",
    },
}


def _definition(provider_key: str) -> dict[str, str]:
    try:
        return _CONFIG[provider_key]
    except KeyError as exc:
        raise ValueError(f"Provider commercial tidak dikenal: {provider_key}") from exc


def configuration(provider_key: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if provider_key == "airbus_oneatlas":
        credential = str(settings.oneatlas_api_key or "").strip()
        workspace = str(settings.oneatlas_workspace_id or "").strip()
        return {
            "provider_key": provider_key,
            "label": "Airbus OneAtlas Data",
            "catalog_url": "https://search.foundation.api.oneatlas.airbus.com/api/v2/opensearch",
            "credential_configured": bool(credential),
            "workspace_configured": bool(workspace),
            "catalog_configured": True,
            "configured": bool(credential and workspace),
            "requires_authentication": True,
        }
    if provider_key == "blacksky_spectra":
        documentation = str(settings.blacksky_spectra_api_contract_reference or "").strip()
        credential = str(settings.blacksky_spectra_credential or "").strip()
        return {
            "provider_key": provider_key,
            "label": "BlackSky Spectra",
            "catalog_url": "",
            "credential_configured": bool(credential),
            "contract_reference_configured": bool(documentation),
            "catalog_configured": False,
            # No API endpoint/auth contract is available, so this is never
            # reported as connected and no network call is enabled.
            "configured": False,
            "requires_authentication": True,
        }
    definition = _definition(provider_key)
    catalog_url = str(getattr(settings, definition["url"], "") or "").strip()
    collection = str(getattr(settings, definition["collection"], "") or "").strip()
    credential = str(getattr(settings, definition["credential"], "") or "").strip()
    scheme = str(getattr(settings, definition["scheme"], "bearer") or "bearer").strip().lower()
    requires_credential = scheme != "none"
    return {
        "provider_key": provider_key,
        "label": definition["label"],
        "catalog_url": catalog_url,
        "collection": collection,
        "credential_configured": bool(credential),
        "catalog_configured": bool(catalog_url),
        "collection_configured": bool(collection),
        "configured": bool(catalog_url and collection and (credential or not requires_credential)),
        "auth_scheme": scheme if scheme in {"bearer", "basic", "none"} else "bearer",
    }


def status(provider_key: str, settings: Settings | None = None) -> dict[str, Any]:
    config = configuration(provider_key, settings)
    if config["configured"]:
        state = "connected"
    elif (
        config["catalog_configured"]
        or config.get("collection_configured")
        or config["credential_configured"]
        or config.get("workspace_configured")
        or config.get("contract_reference_configured")
    ):
        state = "incomplete_configuration"
    else:
        state = "not_configured"
    result = {
        "provider_key": provider_key,
        "status": state,
        "catalog_configured": config["catalog_configured"],
        "credential_configured": config["credential_configured"],
        "requires_authentication": True,
    }
    if "workspace_configured" in config:
        result["workspace_configured"] = config["workspace_configured"]
    if "collection_configured" in config:
        result["collection_configured"] = config["collection_configured"]
    if "contract_reference_configured" in config:
        result["contract_reference_configured"] = config["contract_reference_configured"]
        result["connector_available"] = False
    return result


def all_statuses(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    return {key: status(key, settings) for key in COMMERCIAL_PROVIDER_KEYS}


def request_headers(provider_key: str, settings: Settings | None = None) -> dict[str, str]:
    """Return upstream auth headers without ever returning the secret itself."""
    settings = settings or get_settings()
    definition = _definition(provider_key)
    credential = str(getattr(settings, definition["credential"], "") or "").strip()
    scheme = configuration(provider_key, settings)["auth_scheme"]
    headers = {"User-Agent": "SAVEGEO/1.0"}
    if not credential or scheme == "none":
        return headers
    if scheme == "basic":
        encoded = base64.b64encode(f"{credential}:".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    else:
        headers["Authorization"] = f"Bearer {credential}"
    return headers


def provider_meta(provider_key: str, settings: Settings | None = None) -> dict[str, Any]:
    config = configuration(provider_key, settings)
    result = {
        "configuration_status": status(provider_key, settings)["status"],
        "stac_configured": bool(config["catalog_configured"] and config.get("collection_configured")) if provider_key in _CONFIG else False,
        "credential_configured": config["credential_configured"],
    }
    if "collection_configured" in config:
        result["collection_configured"] = config["collection_configured"]
    if "workspace_configured" in config:
        result["workspace_configured"] = config["workspace_configured"]
    if "contract_reference_configured" in config:
        result["contract_reference_configured"] = config["contract_reference_configured"]
    return result
