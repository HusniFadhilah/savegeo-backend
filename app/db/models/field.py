"""A named, persisted farm/field boundary for Crop Monitoring.

Shape copied from `company_boundary.py` (flat `name + geojson JSONB + area_ha +
timestamps`), minus company-specific columns, plus crop metadata. Deliberately
**no owner/login column** - Crop Monitoring is a Sidebar tab like Carbon/
LC-Change (no auth), not a gated route like the Disaster Dashboard, so Fields
are a shared, publicly-writable list (every visitor sees/edits the same set),
not per-user. Geometry is immutable after creation - editing a Field only
touches its metadata columns (name/commodity/variety/planting_date/
season_label); redrawing the boundary means creating a new Field row, which
keeps `area_ha` always consistent with `geojson` without a recompute-on-edit
code path.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, Float, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Field(Base):
    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    area_ha: Mapped[float] = mapped_column(Float, nullable=False)
    commodity: Mapped[str] = mapped_column(String(50), nullable=False)
    variety: Mapped[str | None] = mapped_column(String(100))
    planting_date: Mapped[dt.date | None] = mapped_column(Date)
    season_label: Mapped[str | None] = mapped_column(String(50))
    # Derived at create/update time from planting_date + crop_registry's growth
    # duration for the commodity - stored so the Field list can show it without
    # recomputing per row; recomputed whenever planting_date or commodity changes.
    estimated_harvest_date: Mapped[dt.date | None] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), onupdate=lambda: dt.datetime.now(dt.timezone.utc)
    )

    def to_dict(self, include_geojson: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "area_ha": self.area_ha,
            "commodity": self.commodity,
            "variety": self.variety,
            "planting_date": self.planting_date.isoformat() if self.planting_date else None,
            "season_label": self.season_label,
            "estimated_harvest_date": self.estimated_harvest_date.isoformat() if self.estimated_harvest_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_geojson:
            data["geojson"] = self.geojson
        return data
