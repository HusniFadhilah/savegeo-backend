"""Read-side repository for the `satellite_providers` admin-override table.

Same pattern as dataset_repo.py: the static registry
(app/registries/satellite_provider_registry.py) is now only the zero-DB-row
fallback/seed - every field a row supplies (including the GEE collection id
and the band-role map) wins over the registry default. If no DB row exists
for a key, the static registry value is used unchanged, so this table needs
no seeding to work.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.satellite_provider_entry import SatelliteProviderEntry
from app.registries.satellite_provider_registry import SATELLITE_PROVIDERS, resolve_satellite

_OVERRIDE_FIELDS = (
    "name", "provider", "resolution_label", "description",
    "gee_collection", "band_role_map",
    "resolution_m", "revisit_days", "swath_km", "launch", "start_year", "bands_available",
)


def get_overrides_by_key(db: Session) -> dict[str, SatelliteProviderEntry]:
    rows = db.query(SatelliteProviderEntry).all()
    return {row.key: row for row in rows}


def apply_override(meta: dict, override: SatelliteProviderEntry | None) -> dict | None:
    """Return an updated copy of `meta`, or None if the override deactivates it."""
    if override is None:
        return meta
    if not override.is_active:
        return None
    merged = dict(meta)
    for field in _OVERRIDE_FIELDS:
        value = getattr(override, field)
        if value:
            merged[field] = value
    return merged


def list_satellites(db: Session) -> dict:
    """Full provider catalog with DB overrides merged in, admin-deactivated
    entries dropped. Used by GET /vegetation/satellites (the picker)."""
    overrides = get_overrides_by_key(db)
    result = {}
    for key, meta in SATELLITE_PROVIDERS.items():
        merged = apply_override(meta, overrides.get(key))
        if merged is not None:
            result[key] = merged
    return result


def get_satellite_meta(db: Session, key: str | None) -> dict:
    """Merged metadata for one provider, used both to build the GEE
    composite (gee_collection/band_role_map) and to populate the `satellite`
    field on analyze responses. Falls back to the raw registry entry if the
    resolved provider was deactivated by an admin - a direct analyze call
    that already names a satellite shouldn't break just because it dropped
    out of the discovery listing."""
    resolved = resolve_satellite(key)
    meta = SATELLITE_PROVIDERS[resolved]
    override = get_overrides_by_key(db).get(resolved)
    merged = apply_override(meta, override)
    return merged if merged is not None else meta
