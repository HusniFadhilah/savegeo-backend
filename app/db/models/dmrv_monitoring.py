"""Versioned monitoring periods bound to an immutable project boundary version."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvMonitoringPeriod(Base):
    __tablename__ = "dmrv_monitoring_periods"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_dmrv_period_project"),
        UniqueConstraint("project_id", "name", name="uq_dmrv_monitoring_period_name"),
        CheckConstraint("end_date >= start_date", name="ck_dmrv_monitoring_period_dates"),
        CheckConstraint("status IN ('planned', 'in_progress', 'completed')", name="ck_dmrv_monitoring_period_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    boundary_version_id: Mapped[str] = mapped_column(ForeignKey("dmrv_boundary_versions.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
