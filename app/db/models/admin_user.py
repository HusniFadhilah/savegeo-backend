from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.core.config import get_settings

if TYPE_CHECKING:
    # Only for the `Mapped["Role | None"]` type hint below - the actual
    # relationship is resolved by SQLAlchemy's mapper registry via the
    # "Role" string, not this import, so it's guarded to avoid a runtime
    # circular import between the two model modules.
    from app.db.models.role import Role


class AdminUser(Base):
    __tablename__ = "admin_users"
    if TYPE_CHECKING:
        _mfa_verified_at: int | None  # request context, deliberately absent from ORM metadata

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Nullable supports databases migrated from older versions. A missing
    # account policy is treated as optional; a superadmin can require MFA.
    mfa_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # A missing role grants nothing; full access is a separate explicit grant.
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    role: Mapped[Role | None] = relationship("Role")

    def to_dict(self) -> dict:
        privileged = bool(self.is_superuser) or any(
            permission.code.endswith(".write") for permission in (self.role.permissions if self.role else [])
        )
        policy_locked = bool(privileged and get_settings().privileged_mfa_required)
        return {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "email": self.email,
            "is_active": self.is_active,
            "mfa_enabled": self.mfa_enabled,
            "mfa_required": self.mfa_required,
            "mfa_required_effective": bool(
                policy_locked
                or self.mfa_required
            ),
            "mfa_policy_locked": policy_locked,
            "is_superuser": bool(self.is_superuser),
            "is_viewer": bool(self.role and self.role.is_viewer),
            "role": self.role.name if self.role else None,
            # Expose only permission codes to the SPA. Credential values and
            # other sensitive fields remain server-side; this is used solely
            # for role-aware navigation and client-side UX.
            "permissions": [p.code for p in self.role.permissions] if self.role else [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }

