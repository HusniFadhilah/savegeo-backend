"""Durable provenance record for every asynchronous analysis job."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalysisProvenance(Base):
    __tablename__ = "analysis_provenance"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    dataset_id: Mapped[str | None] = mapped_column(String(255))
    dataset_version: Mapped[str | None] = mapped_column(String(128))
    acquisition_date: Mapped[str | None] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    aoi_checksum: Mapped[str | None] = mapped_column(String(64))
    crs: Mapped[str | None] = mapped_column(String(128))
    resolution: Mapped[float | None] = mapped_column()
    cloud_mask: Mapped[dict | None] = mapped_column(JSONB)
    model_id: Mapped[str | None] = mapped_column(String(255))
    model_version: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality_flags: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[dict | None] = mapped_column(JSONB)
    artifact_checksums: Mapped[dict | None] = mapped_column(JSONB)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "analysis_type": self.analysis_type,
            "status": self.status,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "acquisition_date": self.acquisition_date,
            "parameters": self.parameters,
            "aoi_checksum": self.aoi_checksum,
            "crs": self.crs,
            "resolution": self.resolution,
            "cloud_mask": self.cloud_mask,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_code": self.error_code,
            "provenance": self.provenance,
            "quality_flags": self.quality_flags,
            "confidence": self.confidence,
            "artifact_checksums": self.artifact_checksums,
            "created_by_user_id": self.created_by_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
