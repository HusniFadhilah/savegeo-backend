"""dMRV project and project membership. Access is granted by membership only."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvProject(Base):
    __tablename__ = "dmrv_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    organization: Mapped[str | None] = mapped_column(String(255))
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ARR")
    jurisdiction: Mapped[str | None] = mapped_column(String(128))
    methodology_id: Mapped[str | None] = mapped_column(String(64))
    methodology_version: Mapped[str | None] = mapped_column(String(32))
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    crediting_period_years: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ProjectMember(Base):
    __tablename__ = "dmrv_project_members"
    __table_args__ = (UniqueConstraint("project_id", "principal_type", "principal_id", name="uq_dmrv_member_principal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    principal_type: Mapped[str] = mapped_column(String(8), nullable=False)  # "user" | "admin"
    principal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    added_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
