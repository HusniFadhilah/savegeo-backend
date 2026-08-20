"""Disaster Intelligence Dashboard - root entity. Everything else (AOI,
imagery, analysis runs/results, hotspots) hangs off a DisasterEvent by FK.
See `savegeo/backend/docs/` redesign plan for the full entity relationship
(DisasterEvent -> AOI / Imagery / AnalysisRuns / Results / Hotspots).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DISASTER_TYPES = (
    "flood", "landslide", "forest_fire", "earthquake", "tsunami",
    "volcanic_eruption", "storm", "drought", "other",
)
EVENT_STATUSES = ("draft", "processing", "ready_for_review", "published", "archived")
SEVERITIES = ("low", "medium", "high", "critical")


class DisasterEvent(Base):
    __tablename__ = "disaster_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    disaster_type: Mapped[str] = mapped_column(String(30), nullable=False)  # see DISASTER_TYPES
    location_name: Mapped[str | None] = mapped_column(String(255))
    province: Mapped[list | None] = mapped_column(JSONB)  # list[str]
    district: Mapped[list | None] = mapped_column(JSONB)  # list[str]
    event_date: Mapped[dt.date | None] = mapped_column(Date)
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)  # see EVENT_STATUSES
    severity: Mapped[str | None] = mapped_column(String(10))  # see SEVERITIES
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(255))
    thumbnail: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), onupdate=lambda: dt.datetime.now(dt.timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "disaster_type": self.disaster_type,
            "location_name": self.location_name,
            "province": self.province or [],
            "district": self.district or [],
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "status": self.status,
            "severity": self.severity,
            "description": self.description,
            "source": self.source,
            "thumbnail": self.thumbnail,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
