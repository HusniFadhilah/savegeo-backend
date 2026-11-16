"""Project integrity workspace. Changes are retained as immutable revisions."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvIntegrityRecord(Base):
    __tablename__ = "dmrv_integrity_records"
    __table_args__ = (UniqueConstraint("project_id", "category", "record_key", name="uq_dmrv_integrity_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    record_key: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(32))
    review_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class DmrvIntegrityRevision(Base):
    __tablename__ = "dmrv_integrity_revisions"
    __table_args__ = (UniqueConstraint("record_id", "revision", name="uq_dmrv_integrity_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    record_id: Mapped[str] = mapped_column(
        ForeignKey("dmrv_integrity_records.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class DmrvIntegrityDocument(Base):
    __tablename__ = "dmrv_integrity_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class DmrvRiskPolicy(Base):
    __tablename__ = "dmrv_risk_policies"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("dmrv_projects.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    tree_loss_threshold_ha: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    min_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str] = mapped_column(String(32), nullable=False)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_enqueued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
