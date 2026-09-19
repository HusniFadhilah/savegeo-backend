"""Persisted satellite hotspot observations for the wildfire explorer.

These rows are deliberately separate from ``hotspots``. The latter is an
admin-curated critical-impact-area table, while this table stores normalized
NASA FIRMS observations that can be refreshed and deduplicated safely.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WildfireHotspot(Base):
    __tablename__ = "wildfire_hotspots"
    __table_args__ = (
        UniqueConstraint("event_id", "external_id", name="uq_wildfire_hotspots_event_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(180), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    acquired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    stats: Mapped[dict | None] = mapped_column(JSONB)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
        nullable=False,
    )

    def to_feature(self) -> dict:
        properties = dict(self.stats or {})
        properties.setdefault("source", self.source)
        properties.setdefault("is_near_real_time", False)
        return {
            "type": "Feature",
            "id": self.external_id,
            "geometry": self.geojson,
            "properties": properties,
        }
