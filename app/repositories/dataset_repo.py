"""Read-side repository for the `datasets` admin-override table.

Pattern: static registries (app/registries/*.py) remain the source of truth
for everything algorithmic (GEE asset IDs, loader functions, band math) - this
repository only answers two questions services need when listing datasets:
  1. is this dataset still active (admin may have deactivated it)?
  2. has an admin overridden its display metadata (name/description/attribution)?

If no DB row exists for a key, the static registry value is used unchanged -
this is the fallback required by the migration plan's "Tahap 3": DB-backed
where present, hardcoded registry otherwise. Deleting/never-seeding this table
does not break dataset listing, it just means no overrides are applied.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.dataset_entry import DatasetEntry

_OVERRIDE_FIELDS = ("name", "full_name", "description", "attribution", "limitations")


def get_overrides_by_key(db: Session, module: str) -> dict[str, DatasetEntry]:
    rows = db.query(DatasetEntry).filter_by(module=module).all()
    return {row.key: row for row in rows}


def apply_override(meta: dict, override: DatasetEntry | None) -> dict | None:
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
