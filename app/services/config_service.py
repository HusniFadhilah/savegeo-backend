"""`system_config` read/write with a 30s in-process TTL cache.

Replaces the legacy Flask pattern of mutating global `app.config` on every admin
config change (`_sync_config_from_db` / `_reload_app_config` in the old app.py /
admin_routes.py). Here, routes read through `get_setting()` / `get_all_settings()`
which self-refresh every `_CFG_TTL` seconds; `invalidate()` is called right after
an admin PUT/reset so changes are visible immediately without waiting for the TTL.
"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.default_configs import DEFAULT_CONFIGS, is_secret_key
from app.db.models.system_config import SystemConfig

_cache: dict[str, SystemConfig] = {}
_cache_ts: float = 0.0
_CFG_TTL = 30.0


def _refresh(db: Session) -> None:
    global _cache, _cache_ts
    rows = db.query(SystemConfig).all()
    _cache = {row.key: row for row in rows}
    _cache_ts = time.time()


def invalidate() -> None:
    global _cache_ts
    _cache_ts = 0.0


def _ensure_fresh(db: Session) -> None:
    if time.time() - _cache_ts >= _CFG_TTL or not _cache:
        _refresh(db)


def get_setting(db: Session, key: str, default=None):
    _ensure_fresh(db)
    row = _cache.get(key)
    return row.typed_value() if row else default


def get_all_settings(db: Session, category: str | None = None) -> list[SystemConfig]:
    _ensure_fresh(db)
    rows = list(_cache.values())
    if category:
        rows = [r for r in rows if r.category == category]
    return rows


def mask_config_dict(cfg: SystemConfig) -> dict:
    data = cfg.to_dict()
    if is_secret_key(cfg.key):
        is_configured = bool(cfg.value)
        data.pop("value", None)
        data.pop("raw_value", None)
        data["is_secret"] = True
        data["is_configured"] = is_configured
    return data


def upsert_setting(db: Session, key: str, value, updated_by: int | None = None) -> SystemConfig:
    row = db.query(SystemConfig).filter_by(key=key).first()
    if row is None:
        # Unknown key — infer defaults from DEFAULT_CONFIGS if present, else generic string config.
        defaults = next((c for c in DEFAULT_CONFIGS if c[0] == key), None)
        row = SystemConfig(
            key=key,
            value_type=defaults[2] if defaults else "string",
            category=defaults[3] if defaults else "general",
            label=defaults[4] if defaults else None,
            description=defaults[5] if defaults else None,
            is_public=defaults[6] if defaults else False,
        )
        db.add(row)

    # Secrets: an empty incoming value is a no-op (prevents a masked GET -> PUT
    # roundtrip in the admin UI from silently wiping a configured secret).
    if is_secret_key(key) and (value is None or value == ""):
        return row

    row.value = str(value) if not isinstance(value, str) else value
    row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    invalidate()
    return row


def get_analysis_defaults(db: Session) -> dict:
    """Resolve GEE analysis defaults (cloud threshold, scales, carbon vis params,
    CO2 factor, max pixels) from `system_config` - the table admins actually edit
    via PUT /api/admin/config - falling back to `app.core.config.Settings` (env
    vars) only if a DB row is somehow missing.

    Previously the analysis services (carbon/vegetation/landcover/download)
    read these directly from `Settings`, which meant an admin editing
    "Carbon vis min/max" or "Cloud threshold (%)" in the admin panel had no
    effect on actual analysis runs - the DB write succeeded but nothing ever
    read it back. This function is the fix: it is the single place analysis
    code should get these values from.
    """
    settings = get_settings()
    palette_raw = get_setting(db, "carbon.vis_palette", settings.carbon_vis_palette)
    return {
        "cloud_threshold": int(get_setting(db, "analysis.cloud_threshold", settings.default_cloud_threshold)),
        "carbon_scale": int(get_setting(db, "analysis.carbon_scale", settings.default_carbon_scale)),
        "veg_scale": int(get_setting(db, "analysis.veg_scale", settings.default_vegetation_scale)),
        "lc_scale": int(get_setting(db, "analysis.lc_scale", settings.default_landcover_scale)),
        "max_pixels": int(float(get_setting(db, "analysis.max_pixels", settings.max_pixels))),
        "carbon_vis_min": float(get_setting(db, "carbon.vis_min", settings.carbon_vis_min)),
        "carbon_vis_max": float(get_setting(db, "carbon.vis_max", settings.carbon_vis_max)),
        "carbon_vis_palette": palette_raw.split(",") if isinstance(palette_raw, str) else list(palette_raw),
        "carbon_co2_factor": float(get_setting(db, "carbon.co2_factor", settings.carbon_co2_factor)),
    }


def reset_setting(db: Session, key: str | None = None) -> int:
    """Reset one key (or all keys if `key` is None) back to DEFAULT_CONFIGS baseline."""
    targets = [c for c in DEFAULT_CONFIGS if key is None or c[0] == key]
    count = 0
    for k, value, value_type, category, label, description, is_public in targets:
        row = db.query(SystemConfig).filter_by(key=k).first()
        if row is None:
            row = SystemConfig(key=k)
            db.add(row)
        row.value = value
        row.value_type = value_type
        row.category = category
        row.label = label
        row.description = description
        row.is_public = is_public
        count += 1
    db.commit()
    invalidate()
    return count
