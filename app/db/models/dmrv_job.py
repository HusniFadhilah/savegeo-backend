"""dMRV durable jobs (PostgreSQL-backed queue) and immutable calculation snapshots."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvJob(Base):
    __tablename__ = "dmrv_jobs"
    __table_args__ = (
        UniqueConstraint("created_by", "idempotency_key", name="uq_dmrv_job_idempotency"),
        Index("ix_dmrv_jobs_status_run_after", "status", "run_after"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")  # queued|running|succeeded|failed|cancelled
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)  # {"code", "message", "retryable"}; never a traceback
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DmrvSnapshot(Base):
    """Immutable: written once, never updated. Identified by a hash over kind, algorithm, inputs and outputs."""

    __tablename__ = "dmrv_snapshots"
    __table_args__ = (UniqueConstraint("project_id", "snapshot_hash", name="uq_dmrv_snapshot_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("dmrv_jobs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    outputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
