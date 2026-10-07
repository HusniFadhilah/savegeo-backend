"""Resolve admin-managed commercial imagery connection settings safely."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.services import config_service
from app.services.commercial_imagery_service import COMMERCIAL_PROVIDER_KEYS, configuration as commercial_configuration

_CONFIG_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "planet_commercial": {
        "catalog_url": ("imagery.planet.stac_url", "planet_stac_url"),
        "collection": ("imagery.planet.collection", "planet_stac_collection"),
        "credential": ("imagery.planet.api_key", "planet_api_key"),
        "auth_scheme": ("imagery.planet.auth_scheme", "planet_auth_scheme"),
    },
    "vantor_commercial": {
        "catalog_url": ("imagery.vantor.stac_url", "vantor_stac_url"),
        "collection": ("imagery.vantor.collection", "vantor_stac_collection"),
        "credential": ("imagery.vantor.api_token", "vantor_api_token"),
        "auth_scheme": ("imagery.vantor.auth_scheme", "vantor_auth_scheme"),
    },
    "iceye_commercial": {
        "catalog_url": ("imagery.iceye.stac_url", "iceye_stac_url"),
        "collection": ("imagery.iceye.collection", "iceye_stac_collection"),
        "credential": ("imagery.iceye.api_token", "iceye_api_token"),
        "auth_scheme": ("imagery.iceye.auth_scheme", "iceye_auth_scheme"),
    },
    "airbus_oneatlas": {
        "credential": ("imagery.oneatlas.api_key", "oneatlas_api_key"),
        "workspace_id": ("imagery.oneatlas.workspace_id", "oneatlas_workspace_id"),
    },
    "blacksky_spectra": {
        "credential": ("imagery.blacksky.credential", "blacksky_spectra_credential"),
        "api_contract_reference": (
            "imagery.blacksky.api_contract_reference", "blacksky_spectra_api_contract_reference"
        ),
    },
}


def effective_settings(db: Session | None = None) -> Settings:
    """Create a request-local settings copy with system_config overrides."""
    settings = get_settings()
    if db is None:
        return settings
    updates: dict[str, Any] = {}
    for provider_fields in _CONFIG_FIELDS.values():
        for key, setting_field in provider_fields.values():
            if setting_field in updates:
                continue
            updates[setting_field] = config_service.get_setting(db, key, getattr(settings, setting_field))
    return settings.model_copy(update=updates)


def admin_connection_view(provider_key: str, settings: Settings) -> dict[str, Any]:
    if provider_key not in COMMERCIAL_PROVIDER_KEYS:
        return {"supported": False}
    source = commercial_configuration(provider_key, settings)
    fields: dict[str, Any] = {}
    for field in _CONFIG_FIELDS[provider_key]:
        if field == "credential":
            continue
        if field == "api_contract_reference":
            fields[field] = str(getattr(settings, "blacksky_spectra_api_contract_reference", "") or "")
        elif field == "workspace_id":
            fields[field] = str(getattr(settings, "oneatlas_workspace_id", "") or "")
        elif field == "catalog_url":
            fields[field] = source.get("catalog_url", "")
        elif field == "collection":
            fields[field] = source.get("collection", "")
        elif field == "auth_scheme":
            fields[field] = source.get("auth_scheme", "bearer")
    state = (
        "blocked" if provider_key == "blacksky_spectra"
        else "connected" if source.get("configured")
        else "incomplete_configuration" if any((
            source.get("catalog_configured"), source.get("collection_configured"),
            source.get("credential_configured"), source.get("workspace_configured"),
            source.get("contract_reference_configured"),
        ))
        else "not_configured"
    )
    return {
        "supported": True,
        "fields": fields,
        "credential_configured": bool(source.get("credential_configured")),
        "status": state,
        "connector_available": source.get("catalog_configured", False) if provider_key != "blacksky_spectra" else False,
        "note": (
            "BlackSky Spectra belum memiliki kontrak/API pelanggan resmi di konektor SAVEGEO; menyimpan referensi atau credential tidak mengaktifkan akses."
            if provider_key == "blacksky_spectra" else None
        ),
    }


def save_connection(db: Session, provider_key: str, payload: dict[str, Any], updated_by: int) -> list[str]:
    if provider_key not in _CONFIG_FIELDS:
        raise KeyError(provider_key)
    if set(payload) - {"fields", "credential", "clear_credential"}:
        raise HTTPException(status_code=422, detail="Ada field koneksi yang tidak didukung")
    fields = payload.get("fields") or {}
    if not isinstance(fields, dict):
        raise HTTPException(status_code=422, detail="fields harus berupa objek")
    if set(fields) - (set(_CONFIG_FIELDS[provider_key]) - {"credential"}):
        raise HTTPException(status_code=422, detail="Ada field konfigurasi yang tidak didukung provider ini")
    if "credential" in payload and not isinstance(payload["credential"], str):
        raise HTTPException(status_code=422, detail="Credential harus berupa teks")
    if "clear_credential" in payload and not isinstance(payload["clear_credential"], bool):
        raise HTTPException(status_code=422, detail="clear_credential harus bernilai true atau false")
    if payload.get("credential") and payload.get("clear_credential"):
        raise HTTPException(status_code=422, detail="Credential baru dan penghapusan credential tidak dapat dikirim bersamaan")
    if len(str(payload.get("credential") or "")) > 4096:
        raise HTTPException(status_code=422, detail="Credential terlalu panjang")

    updates: list[str] = []
    for field, value in fields.items():
        config_key = _CONFIG_FIELDS[provider_key][field][0]
        if not isinstance(value, str) or len(value) > 2048:
            raise HTTPException(status_code=422, detail=f"Nilai {field} tidak valid")
        value = value.strip()
        if field == "catalog_url" and value:
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(status_code=422, detail="URL katalog komersial harus HTTPS tanpa username/password")
        if field == "auth_scheme" and value.lower() not in {"basic", "bearer", "none"}:
            raise HTTPException(status_code=422, detail="Auth scheme harus basic, bearer, atau none")
        if field == "workspace_id" and any(char.isspace() for char in value):
            raise HTTPException(status_code=422, detail="Workspace ID tidak boleh mengandung spasi")
        config_service.upsert_setting(db, config_key, value, updated_by=updated_by)
        updates.append(config_key)

    credential = payload.get("credential")
    if credential:
        config_key = _CONFIG_FIELDS[provider_key]["credential"][0]
        config_service.upsert_setting(db, config_key, credential.strip(), updated_by=updated_by)
        updates.append(config_key)
    elif payload.get("clear_credential"):
        config_key = _CONFIG_FIELDS[provider_key]["credential"][0]
        config_service.clear_secret_setting(db, config_key, updated_by=updated_by)
        updates.append(config_key)
    return updates
