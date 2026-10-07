"""Database overlay for the raw imagery provider catalog."""
from __future__ import annotations

import threading
from importlib.util import find_spec
from typing import Any

from sqlalchemy.orm import Session

from app.db.models.imagery_provider_entry import ImageryProviderEntry
from app.registries.imagery_provider_registry import IMAGERY_PROVIDERS

_EDITABLE_FIELDS = {
    "name", "provider", "group", "description", "resolution_m", "revisit_days", "start_year",
    "gee_collection", "cloud_property",
}
_cache_lock = threading.RLock()
_cached_overrides: dict[str, dict[str, Any]] = {}


def provider_catalog() -> dict[str, dict[str, Any]]:
    """Static provider definitions, including optional CDSE connectors when installed."""
    catalog = dict(IMAGERY_PROVIDERS)
    if find_spec("geosave_engine") is not None:
        try:
            from app.services.copernicus_geosave_service import COPERNICUS_PROVIDERS
            catalog.update(COPERNICUS_PROVIDERS)
        except (ImportError, ModuleNotFoundError):
            pass
    return catalog


def refresh_cache(db: Session) -> dict[str, dict[str, Any]]:
    """Publish a fresh, immutable-by-convention snapshot for imagery workers."""
    overrides = {
        row.key: {
            "is_active": row.is_active,
            "display_order": row.display_order,
            "overrides": dict(row.overrides or {}),
        }
        for row in db.query(ImageryProviderEntry).all()
    }
    with _cache_lock:
        _cached_overrides.clear()
        _cached_overrides.update(overrides)
        return dict(_cached_overrides)


def cached_overrides() -> dict[str, dict[str, Any]]:
    with _cache_lock:
        return dict(_cached_overrides)


def apply_cached_override(key: str, meta: dict[str, Any]) -> dict[str, Any]:
    row = cached_overrides().get(key)
    return {**meta, **(row or {}).get("overrides", {})}


def list_provider_rows(db: Session, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    overrides = refresh_cache(db)
    rows: list[dict[str, Any]] = []
    for index, (key, meta) in enumerate(provider_catalog().items()):
        row = overrides.get(key, {})
        if not include_inactive and not row.get("is_active", True):
            continue
        rows.append({
            **meta,
            **row.get("overrides", {}),
            "key": key,
            "is_active": row.get("is_active", True),
            "display_order": row.get("display_order") if row.get("display_order") is not None else index,
            "has_override": key in overrides,
        })
    return sorted(rows, key=lambda item: (item["display_order"], item["key"]))


def update_provider(db: Session, key: str, patch: dict[str, Any]) -> ImageryProviderEntry:
    if key not in provider_catalog():
        raise KeyError(key)
    row = db.get(ImageryProviderEntry, key)
    if row is None:
        row = ImageryProviderEntry(key=key, is_active=True, overrides={})
        db.add(row)
    overrides = dict(row.overrides or {})
    for field, value in patch.items():
        if field in _EDITABLE_FIELDS:
            overrides[field] = value
        elif field == "is_active":
            row.is_active = value
        elif field == "display_order":
            row.display_order = value
    row.overrides = overrides or None
    db.commit()
    db.refresh(row)
    refresh_cache(db)
    return row


def reset_provider(db: Session, key: str) -> bool:
    if key not in provider_catalog():
        raise KeyError(key)
    row = db.get(ImageryProviderEntry, key)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    refresh_cache(db)
    return True


def provider_is_active(key: str) -> bool:
    return cached_overrides().get(key, {}).get("is_active", True)


def provider_meta(key: str) -> dict[str, Any]:
    meta = provider_catalog().get(key)
    if meta is None:
        raise KeyError(key)
    return apply_cached_override(key, meta)
