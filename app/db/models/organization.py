"""Customer workspaces. Membership is independent of global and project roles."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    personal_owner: Mapped[str | None] = mapped_column(String(32), unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "principal_type", "principal_id", name="uq_org_member_principal"),
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_org_member_role"),
        CheckConstraint("principal_type IN ('user', 'admin')", name="ck_org_member_principal_type"),
        Index("ix_org_members_principal_active", "principal_type", "principal_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    principal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class OrganizationInvitation(Base):
    __tablename__ = "organization_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceRequest(Base):
    """Replay record for workspace mutations; token-bearing replies are excluded."""

    __tablename__ = "workspace_requests"
    __table_args__ = (UniqueConstraint("principal_key", "key", name="uq_workspace_request_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_key: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
