"""Immutable output registry for geospatial actions and analysis jobs."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.temporal import format_temporal
from app.db.base import Base


class AnalysisOutput(Base):
    __tablename__ = "analysis_outputs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_provenance.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    profile: Mapped[str | None] = mapped_column(String(128))
    href: Mapped[str] = mapped_column(Text, nullable=False)
    storage_location: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    feature_count: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), nullable=False
    )

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "action_id": self.action_id,
            "title": self.title,
            "role": self.role,
            "media_type": self.media_type,
            "profile": self.profile,
            "href": self.href,
            "storage_location": self.storage_location,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "feature_count": self.feature_count,
            "metadata": self.metadata_json or {},
            "version": self.version,
            "status": self.status,
            "expires_at": format_temporal(self.expires_at, field="expires_at"),
            "created_at": format_temporal(self.created_at, field="created_at"),
        }
        return data
