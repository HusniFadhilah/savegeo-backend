"""Admin-curated "Critical Impact Area" markers (spec section 27). Manually
authored by Admin against an existing published AnalysisResult - not an
auto-clustering ML output. User can view/zoom/highlight only, never edit.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

IMPACT_LEVELS = ("low", "medium", "high", "critical")


class Hotspot(Base):
    __tablename__ = "hotspots"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False, index=True)
    analysis_result_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_results.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    impact_level: Mapped[str] = mapped_column(String(10), nullable=False)  # see IMPACT_LEVELS
    geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)  # point or polygon
    stats: Mapped[dict | None] = mapped_column(JSONB)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "analysis_result_id": self.analysis_result_id,
            "name": self.name,
            "impact_level": self.impact_level,
            "geojson": self.geojson,
            "stats": self.stats,
            "is_published": self.is_published,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
