"""Admin-defined Area of Interest for a disaster event. One event can only
ever have AOIs created/updated by Admin (aoi:create/aoi:update permissions) -
User routes only ever read the active AOI, never write it.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AOI_SOURCES = ("draw", "upload_geojson", "upload_shp", "upload_kml", "admin_boundary", "existing")


class DisasterAOI(Base):
    __tablename__ = "disaster_aois"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False, index=True)
    geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    area_ha: Mapped[float | None] = mapped_column(Float)
    centroid: Mapped[dict | None] = mapped_column(JSONB)  # {"lat": .., "lng": ..}
    bbox: Mapped[list | None] = mapped_column(JSONB)  # [minLng, minLat, maxLng, maxLat]
    source: Mapped[str] = mapped_column(String(30), default="draw")  # see AOI_SOURCES
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    def to_dict(self, include_geojson: bool = True) -> dict:
        data = {
            "id": self.id,
            "event_id": self.event_id,
            "area_ha": self.area_ha,
            "centroid": self.centroid,
            "bbox": self.bbox,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_geojson:
            data["geojson"] = self.geojson
        return data
