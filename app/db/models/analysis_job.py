"""Durable owner-scoped analysis work, independent of web worker lifetimes."""
from sqlalchemy import Boolean, CheckConstraint, Float, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        UniqueConstraint("owner", "idempotency_key", name="uq_analysis_jobs_idempotency"),
        CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_analysis_jobs_attempts"),
        Index("ix_analysis_jobs_claim", "status", "created_at"),
        Index("ix_analysis_jobs_lease", "lease_expires_at"),
        Index("ix_analysis_jobs_owner", "owner", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="preparing")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    cache_key: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[float | None] = mapped_column(Float)
    heartbeat_at: Mapped[float | None] = mapped_column(Float)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)
