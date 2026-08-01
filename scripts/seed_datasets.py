"""Seed the `datasets` admin-override table from the static registries
(carbon_dataset_registry.py, landcover_dataset_registry.py). Idempotent -
skips keys that already have a row (an admin may have already edited it).

This does NOT replace the registries - GEE asset IDs / loader functions stay
in code. This table only seeds the display/business metadata layer that
app/repositories/dataset_repo.py overlays on top of the registry at read time.

Usage: python -m scripts.seed_datasets
"""
from __future__ import annotations

from app.db.models.dataset_entry import DatasetEntry
from app.db.session import SessionLocal
from app.registries.carbon_dataset_registry import (
    CARBON_ARCGIS_REGISTRY,
    CARBON_DATASET_REGISTRY,
    CARBON_EXTERNAL_REGISTRY,
)
from app.registries.landcover_dataset_registry import LAND_COVER_DATASET_OPTIONS


def _year_bounds(meta: dict) -> tuple[int | None, int | None]:
    yr = meta.get("year_range") or meta.get("year")
    if isinstance(yr, (list, tuple)) and len(yr) == 2:
        return yr[0], yr[1]
    if isinstance(yr, int):
        return yr, yr
    return meta.get("year_min"), meta.get("year_max")


def seed_datasets() -> None:
    db = SessionLocal()
    try:
        existing_keys = {row.key for row in db.query(DatasetEntry.key).all()}
        created = 0

        carbon_sources = [
            (CARBON_DATASET_REGISTRY, "gee"),
            (CARBON_ARCGIS_REGISTRY, "arcgis_living_atlas"),
            (CARBON_EXTERNAL_REGISTRY, "external_raster"),
        ]
        for registry, default_provider in carbon_sources:
            for key, meta in registry.items():
                if key in existing_keys:
                    continue
                year_min, year_max = _year_bounds(meta)
                db.add(DatasetEntry(
                    key=key,
                    module="carbon",
                    provider_type=meta.get("provider_type", default_provider),
                    name=meta.get("name"),
                    full_name=meta.get("full_name"),
                    description=meta.get("description"),
                    attribution=meta.get("attribution"),
                    limitations=meta.get("limitations") or [],
                    unit=meta.get("unit"),
                    resolution=str(meta.get("resolution")) if meta.get("resolution") is not None else None,
                    year_min=year_min,
                    year_max=year_max,
                    is_active=True,
                ))
                existing_keys.add(key)
                created += 1

        for key, meta in LAND_COVER_DATASET_OPTIONS.items():
            if key in existing_keys:
                continue
            db.add(DatasetEntry(
                key=key,
                module="landcover",
                provider_type=meta.get("provider_type", "gee_official"),
                name=meta.get("name"),
                full_name=meta.get("name"),
                description=meta.get("description"),
                attribution=meta.get("attribution"),
                limitations=meta.get("limitations") or [],
                unit=None,
                resolution=str(meta.get("resolution")) if meta.get("resolution") is not None else None,
                year_min=meta.get("year_min"),
                year_max=meta.get("year_max") if isinstance(meta.get("year_max"), int) else None,
                is_active=True,
            ))
            existing_keys.add(key)
            created += 1

        db.commit()
        print(f"Seeded {created} new dataset rows ({len(existing_keys) - created} already existed).")
    finally:
        db.close()


if __name__ == "__main__":
    seed_datasets()
