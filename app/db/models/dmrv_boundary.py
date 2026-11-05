"""Append-only project boundary versions (original + validated geometry)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BoundaryVersion(Base):
    __tablename__ = "dmrv_boundary_versions"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_dmrv_boundary_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validated_geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    crs: Mapped[str] = mapped_column(String(32), nullable=False, default="EPSG:4326")
    area_ha: Mapped[float] = mapped_column(Float, nullable=False)
    area_method: Mapped[str] = mapped_column(String(64), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
