from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetEntry(Base):
    """Admin-editable overlay on top of the static dataset registries
    (app/registries/carbon_dataset_registry.py etc). GEE asset IDs, band
    names, and loader functions stay in code (algorithmic/library-bound,
    see docs/hardcoded-data-audit.md classification) - this table only holds
    the display/business metadata an admin plausibly wants to change without a
    redeploy: whether a dataset is active, its shown name/description, and
    attribution/limitations text.

    `key` matches the registry dict key (e.g. "WCMC", "ESA_WorldCover") so a
    row is looked up by key and merged over the static registry entry -
    see app/repositories/dataset_repo.py.
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(32), nullable=False)  # carbon|landcover|vegetation|maps
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    attribution: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list | None] = mapped_column(JSONB)
    unit: Mapped[str | None] = mapped_column(String(64))
    resolution: Mapped[str | None] = mapped_column(String(32))
    year_min: Mapped[int | None] = mapped_column(Integer)
    year_max: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_metadata: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "module": self.module,
            "provider_type": self.provider_type,
            "name": self.name,
            "full_name": self.full_name,
            "description": self.description,
            "attribution": self.attribution,
            "limitations": self.limitations or [],
            "unit": self.unit,
            "resolution": self.resolution,
            "year_min": self.year_min,
            "year_max": self.year_max,
            "is_active": self.is_active,
            "display_order": self.display_order,
        }

    def overrides(self) -> dict:
        """Non-null fields only, for merging over a static registry dict."""
        data = self.to_dict()
        return {k: v for k, v in data.items() if v not in (None, [], {})} | {"is_active": self.is_active}
