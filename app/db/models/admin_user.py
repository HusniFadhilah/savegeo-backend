from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Nullable and NOT backfilled on existing rows on purpose: NULL means
    # "legacy full-access admin" (the only mode that existed before this
    # column was added), so no existing admin's access changes. Only admins
    # explicitly assigned a role get the narrower, permission-checked path -
    # see app/core/security.py::require_permission.
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    role: Mapped["Role | None"] = relationship("Role")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "is_active": self.is_active,
            "role": self.role.name if self.role else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }
