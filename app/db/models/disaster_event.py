"""Disaster Intelligence Dashboard - root entity. Everything else (AOI,
imagery, analysis runs/results, hotspots) hangs off a DisasterEvent by FK.
See `savegeo/backend/docs/` redesign plan for the full entity relationship
(DisasterEvent -> AOI / Imagery / AnalysisRuns / Results / Hotspots).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
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
    # Standalone wildfire explorer metadata. Kept on the event aggregate so a
    # new event can be created from admin data without a new UI component.
    slug: Mapped[str | None] = mapped_column(String(180), unique=True, index=True)
    short_title: Mapped[str | None] = mapped_column(String(120))
    monitoring_from: Mapped[dt.date | None] = mapped_column(Date)
    monitoring_to: Mapped[dt.date | None] = mapped_column(Date)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_data_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    hotspot_last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    year: Mapped[int | None] = mapped_column(Integer)
    country_code: Mapped[str | None] = mapped_column(String(3), default="ID")
    province_codes: Mapped[list | None] = mapped_column(JSONB)
    city_codes: Mapped[list | None] = mapped_column(JSONB)
    bbox: Mapped[list | None] = mapped_column(JSONB)
    center_lat: Mapped[float | None]
    center_lon: Mapped[float | None]
    default_zoom: Mapped[int | None] = mapped_column(Integer)
    source_ids: Mapped[list | None] = mapped_column(JSONB)
    hotspot_dataset_ids: Mapped[list | None] = mapped_column(JSONB)
    burned_area_dataset_ids: Mapped[list | None] = mapped_column(JSONB)
    boundary_source: Mapped[str | None] = mapped_column(String(255))
    methodology: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list | None] = mapped_column(JSONB)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
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
            "slug": self.slug,
            "short_title": self.short_title or self.name,
            "monitoring_from": self.monitoring_from.isoformat() if self.monitoring_from else self.start_date.isoformat() if self.start_date else None,
            "monitoring_to": self.monitoring_to.isoformat() if self.monitoring_to else self.end_date.isoformat() if self.end_date else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "last_data_at": self.last_data_at.isoformat() if self.last_data_at else None,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "hotspot_last_synced_at": self.hotspot_last_synced_at.isoformat() if self.hotspot_last_synced_at else None,
            "year": self.year or (self.event_date.year if self.event_date else None),
            "country_code": self.country_code or "ID",
            "province_codes": self.province_codes or [],
            "city_codes": self.city_codes or [],
            "bbox": self.bbox,
            "center_lat": self.center_lat,
            "center_lon": self.center_lon,
            "default_zoom": self.default_zoom,
            "source_ids": self.source_ids or [],
            "hotspot_dataset_ids": self.hotspot_dataset_ids or [],
            "burned_area_dataset_ids": self.burned_area_dataset_ids or [],
            "boundary_source": self.boundary_source,
            "methodology": self.methodology,
            "limitations": self.limitations or [],
            "is_featured": self.is_featured,
            "is_public": self.is_public,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
