"""Persistent, reusable administrative green-cover ranking snapshots."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GreenRankingSnapshot(Base):
    __tablename__ = "green_ranking_snapshots"
    __table_args__ = (Index("ix_green_ranking_snapshot_freshness", "refreshed_at"),)

    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_run_id: Mapped[str | None] = mapped_column(String(36))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    statistics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    boundary_source: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    refresh_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GreenRankingSnapshotRow(Base):
    __tablename__ = "green_ranking_snapshot_rows"
    __table_args__ = (
        Index("ix_green_ranking_snapshot_rank", "scope_key", "eligible", "rank"),
        Index("ix_green_ranking_snapshot_green", "scope_key", "eligible", "green_percent"),
    )

    scope_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("green_ranking_snapshots.scope_key", ondelete="CASCADE"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    green_percent: Mapped[float | None] = mapped_column(Float)
    coverage_percent: Mapped[float | None] = mapped_column(Float)
    area_ha: Mapped[float | None] = mapped_column(Float)
    valid_area_ha: Mapped[float | None] = mapped_column(Float)
    green_area_ha: Mapped[float | None] = mapped_column(Float)
    trees_ha: Mapped[float | None] = mapped_column(Float)
    grass_ha: Mapped[float | None] = mapped_column(Float)
    crops_ha: Mapped[float | None] = mapped_column(Float)
    shrubs_ha: Mapped[float | None] = mapped_column(Float)
    flooded_ha: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
