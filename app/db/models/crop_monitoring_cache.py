"""Short-lived, input-specific crop section results, including temporary map IDs."""
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CropMonitoringCache(Base):
    __tablename__ = "crop_monitoring_cache"
    __table_args__ = (Index("ix_crop_monitoring_cache_expiry", "expires_at"),)

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id", ondelete="CASCADE"), nullable=False, index=True)
    section: Mapped[str] = mapped_column(String(40), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
