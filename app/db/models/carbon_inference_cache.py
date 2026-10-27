"""Short-lived cache for completed carbon analyses.

Earth Engine map IDs are temporary, so GEE-backed results are cached only for
the configured map-ID window. Local inference results use a separate TTL.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, String, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CarbonInferenceCache(Base):
    __tablename__ = "carbon_inference_cache"
    __table_args__ = (
        Index("ix_carbon_inference_cache_owner_status", "owner_key", "status"),
        Index("ix_carbon_inference_cache_expires_at", "expires_at"),
    )

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(128), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    result: Mapped[dict | None] = mapped_column(JSONB)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )
