"""Seed `system_config` with default rows (idempotent — skips keys that already exist).

Usage:
    python -m scripts.seed_config
"""
from __future__ import annotations

from app.core.default_configs import DEFAULT_CONFIGS
from app.db.models.system_config import SystemConfig
from app.db.session import SessionLocal


def seed_default_configs() -> None:
    db = SessionLocal()
    try:
        existing_keys = {row.key for row in db.query(SystemConfig.key).all()}
        created = 0
        for key, value, value_type, category, label, description, is_public in DEFAULT_CONFIGS:
            if key in existing_keys:
                continue
            db.add(
                SystemConfig(
                    key=key,
                    value=value,
                    value_type=value_type,
                    category=category,
                    label=label,
                    description=description,
                    is_public=is_public,
                )
            )
            created += 1
        db.commit()
        print(f"Seeded {created} new system_config rows ({len(existing_keys)} already existed).")
    finally:
        db.close()


if __name__ == "__main__":
    seed_default_configs()
