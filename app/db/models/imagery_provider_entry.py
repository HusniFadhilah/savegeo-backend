from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ImageryProviderEntry(Base):
    """Admin-managed overlay for the raw imagery browser provider catalog."""

    __tablename__ = "imagery_provider_entries"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int | None] = mapped_column(Integer)
    overrides: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )
