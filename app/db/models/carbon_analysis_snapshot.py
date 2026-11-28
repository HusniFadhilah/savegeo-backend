"""Append-only statistics and provenance snapshot for a completed carbon run."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CarbonAnalysisSnapshot(Base):
    __tablename__ = "carbon_analysis_snapshots"

    analysis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_provenance.id", ondelete="RESTRICT"), primary_key=True
    )
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), nullable=False
    )
