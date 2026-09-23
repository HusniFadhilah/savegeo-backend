"""One record per admin-triggered model execution against a disaster event.
`model_id` is a string key into `app/registries/disaster_model_registry.py`
(not a FK - registry entries aren't DB rows, matches the static-registry
convention used by carbon/landcover datasets in this codebase).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RUN_STATUSES = ("queued", "processing", "completed", "failed", "review_required", "published", "stale")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)  # key in disaster_model_registry
    model_version: Mapped[str | None] = mapped_column(String(20))
    aoi_id: Mapped[int] = mapped_column(ForeignKey("disaster_aois.id", ondelete="CASCADE"), nullable=False)
    pre_imagery_id: Mapped[int | None] = mapped_column(ForeignKey("satellite_imagery.id", ondelete="SET NULL"))
    post_imagery_id: Mapped[int | None] = mapped_column(ForeignKey("satellite_imagery.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)  # see RUN_STATUSES
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "aoi_id": self.aoi_id,
            "pre_imagery_id": self.pre_imagery_id,
            "post_imagery_id": self.post_imagery_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
            "parameters": self.parameters or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
