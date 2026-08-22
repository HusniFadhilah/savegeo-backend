from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SatelliteProviderEntry(Base):
    """Admin-editable overlay on top of the static satellite provider catalog
    (app/registries/satellite_provider_registry.py), same pattern as
    DatasetEntry for the carbon/landcover registries. `key` must match one of
    the registry's three known providers ("sentinel2"/"landsat8"/"landsat9")
    so a row is looked up by key and merged over the static registry entry -
    see app/repositories/satellite_provider_repo.py. A row can override every
    field, including the GEE collection id and the generic-role->native-band
    map (e.g. point "sentinel2" at a newer harmonized asset) - the registry
    is now only the zero-DB-row fallback/seed, not the sole source of truth.
    All columns nullable; a null column falls back to the static registry
    default. The cloud-masking *algorithm* choice (Sentinel-2 QA60 bitmask vs
    Landsat QA_PIXEL bitmask) still keys off which of the three known
    providers this row overrides, not a DB field - a genuinely new provider
    with a different cloud-mask scheme needs a code change, not just a row.
    """

    __tablename__ = "satellite_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str | None] = mapped_column(String(255))
    resolution_label: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Technical fields (default hardcoded in the registry; overridable here).
    gee_collection: Mapped[str | None] = mapped_column(String(255))
    band_role_map: Mapped[dict | None] = mapped_column(JSONB)  # {"blue": "SR_B2", "red": "SR_B4", ...}
    resolution_m: Mapped[int | None] = mapped_column(Integer)
    revisit_days: Mapped[int | None] = mapped_column(Integer)
    swath_km: Mapped[int | None] = mapped_column(Integer)
    launch: Mapped[str | None] = mapped_column(String(64))
    start_year: Mapped[int | None] = mapped_column(Integer)
    bands_available: Mapped[list | None] = mapped_column(JSONB)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "provider": self.provider,
            "resolution_label": self.resolution_label,
            "description": self.description,
            "is_active": self.is_active,
            "display_order": self.display_order,
            "gee_collection": self.gee_collection,
            "band_role_map": self.band_role_map,
            "resolution_m": self.resolution_m,
            "revisit_days": self.revisit_days,
            "swath_km": self.swath_km,
            "launch": self.launch,
            "start_year": self.start_year,
            "bands_available": self.bands_available,
        }

    def overrides(self) -> dict:
        """Non-null fields only, for merging over a static registry dict."""
        data = self.to_dict()
        return {k: v for k, v in data.items() if v not in (None, [], {})} | {"is_active": self.is_active}
