from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """Append-only record of admin mutations (config/model/credential/dataset
    writes). Insert-only from route handlers - never read/write from
    analysis code paths, so it cannot affect existing endpoint behavior."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "config.update", "model.delete"
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "system_config", "uploaded_model"
    resource_id: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "admin_user_id": self.admin_user_id,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "detail": self.detail,
            "ip_address": self.ip_address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
