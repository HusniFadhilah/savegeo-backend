"""Immutable evidence package manifests and generated archive metadata."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvEvidencePackage(Base):
    __tablename__ = "dmrv_evidence_packages"
    __table_args__ = (
        UniqueConstraint("principal_key", "idempotency_key", name="uq_dmrv_evidence_idempotency"),
        Index("ix_dmrv_evidence_project_created", "project_id", "created_at"),
        CheckConstraint("readiness IN ('incomplete', 'ready')", name="ck_dmrv_evidence_readiness"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("dmrv_snapshots.id", ondelete="RESTRICT"), index=True)
    principal_key: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    missing_components: Mapped[list] = mapped_column(JSONB, nullable=False)
    readiness: Mapped[str] = mapped_column(String(16), nullable=False, default="incomplete")
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
