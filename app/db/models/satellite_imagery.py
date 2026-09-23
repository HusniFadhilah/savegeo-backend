"""Admin-configured pre/post satellite imagery for a disaster event. User
routes only ever pick among rows already inserted here by Admin (spec: no
arbitrary satellite query on the user side).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

IMAGERY_PHASES = ("pre", "post")


class SatelliteImagery(Base):
    __tablename__ = "satellite_imagery"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(10), nullable=False)  # pre|post
    satellite: Mapped[str] = mapped_column(String(50), nullable=False)  # Sentinel-2, Sentinel-1 SAR, Landsat
    acquisition_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    sensor: Mapped[str | None] = mapped_column(String(50))
    resolution_m: Mapped[float | None] = mapped_column(Float)
    cloud_coverage_pct: Mapped[float | None] = mapped_column(Float)
    data_source: Mapped[str | None] = mapped_column(String(100))
    scene_id: Mapped[str | None] = mapped_column(String(255), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    preview_tile_url: Mapped[str | None] = mapped_column(String(1000))
    # "gee" (default, existing behavior - preview_tile_url points at an
    # already-live GEE getMapId() tile template) | "local_upload" (admin-
    # ingested raster served by local_imagery_tile_service via
    # local_file_path - see 0008_local_imagery migration).
    source_kind: Mapped[str] = mapped_column(String(20), default="gee", nullable=False)
    local_file_path: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "phase": self.phase,
            "satellite": self.satellite,
            "acquisition_date": self.acquisition_date.isoformat() if self.acquisition_date else None,
            "sensor": self.sensor,
            "resolution_m": self.resolution_m,
            "cloud_coverage_pct": self.cloud_coverage_pct,
            "data_source": self.data_source,
            "scene_id": self.scene_id,
            "is_primary": self.is_primary,
            "preview_tile_url": self.preview_tile_url,
            "source_kind": self.source_kind,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
