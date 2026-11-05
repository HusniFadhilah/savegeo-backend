"""dMRV audit events (written in the same transaction as the state change) and
idempotency keys for mutating requests."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvAuditEvent(Base):
    __tablename__ = "dmrv_audit_events"
    __table_args__ = (Index("ix_dmrv_audit_project_created", "project_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36))
    actor_type: Mapped[str] = mapped_column(String(8), nullable=False)
    actor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DmrvIdempotencyKey(Base):
    __tablename__ = "dmrv_idempotency_keys"
    __table_args__ = (UniqueConstraint("principal_key", "key", name="uq_dmrv_idempotency_principal_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    principal_key: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
