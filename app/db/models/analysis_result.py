"""1:1 with AnalysisRun. Holds structured statistics (spec section 9 - never
just a raster), optional per-feature GeoJSON for click-to-detail, legend, and
the publish gate (`is_published`). User-facing routes must always filter on
`is_published == True` - unpublished rows are Admin-only (QC/review).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    tile_url: Mapped[str | None] = mapped_column(String(1000))
    statistics: Mapped[dict | None] = mapped_column(JSONB)  # model-specific shape, see spec section 9
    features: Mapped[dict | None] = mapped_column(JSONB)  # GeoJSON FeatureCollection, nullable
    legend: Mapped[list | None] = mapped_column(JSONB)  # [{"label": .., "color": ..}, ...]
    confidence_summary: Mapped[dict | None] = mapped_column(JSONB)  # {"mean": .., "high_pct": .., ...}, nullable
    provenance: Mapped[dict | None] = mapped_column(JSONB)
    validation_status: Mapped[str | None] = mapped_column(String(30))
    limitations: Mapped[list | None] = mapped_column(JSONB)
    stale_reason: Mapped[str | None] = mapped_column(Text)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    publication_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    def to_dict(self, include_features: bool = False) -> dict:
        data = {
            "id": self.id,
            "run_id": self.run_id,
            "tile_url": self.tile_url,
            "statistics": {k: v for k, v in (self.statistics or {}).items() if k not in ("comparison", "cache_key")},
            "comparison": (self.statistics or {}).get("comparison"),
            "legend": self.legend or [],
            "confidence_summary": self.confidence_summary,
            "provenance": self.provenance,
            "validation_status": self.validation_status,
            "limitations": self.limitations or [],
            "stale_reason": self.stale_reason,
            "is_published": self.is_published,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "publication_version": self.publication_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_features:
            data["features"] = self.features
        comparison = (self.statistics or {}).get("comparison")
        if comparison:
            for key in ("model_id", "model_version", "event_id", "aoi_id", "pre_imagery_id", "post_imagery_id",
                        "pre_tile_url", "post_tile_url", "change_tile_url", "confidence_tile_url", "classes"):
                data[key] = comparison.get(key)
        return data
