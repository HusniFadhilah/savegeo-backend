"""Immutable run context and a transactional monthly run quota.

One unit is one submitted analysis run (not hectares or carbon credits).
Provider retries reuse the reservation. Failed/cancelled runs release it.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, Date, DateTime, Float, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkspaceAnalysisRun(Base):
    __tablename__ = "workspace_analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "organization_id"], ["dmrv_projects.id", "dmrv_projects.organization_id"], name="fk_run_project_organization"),
        ForeignKeyConstraint(["job_id", "project_id"], ["dmrv_jobs.id", "dmrv_jobs.project_id"], name="fk_run_job_project"),
        ForeignKeyConstraint(["period_id", "project_id"], ["dmrv_monitoring_periods.id", "dmrv_monitoring_periods.project_id"], name="fk_run_period_project"),
        ForeignKeyConstraint(["boundary_id", "project_id"], ["dmrv_boundary_versions.id", "dmrv_boundary_versions.project_id"], name="fk_run_boundary_project"),
    )

    job_id: Mapped[str] = mapped_column(ForeignKey("dmrv_jobs.id", ondelete="RESTRICT"), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="RESTRICT"), index=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("dmrv_monitoring_periods.id", ondelete="RESTRICT"))
    boundary_id: Mapped[str] = mapped_column(ForeignKey("dmrv_boundary_versions.id", ondelete="RESTRICT"))
    context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class WorkspaceQuota(Base):
    __tablename__ = "workspace_quotas"
    __table_args__ = (
        UniqueConstraint("organization_id", "cycle_start", name="uq_workspace_quota_cycle"),
        CheckConstraint("run_limit >= 0 AND reserved >= 0 AND used >= 0 AND reserved + used <= run_limit", name="ck_workspace_quota_balance"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    cycle_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    run_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="analysis_run_v1")


class WorkspaceUsageEvent(Base):
    __tablename__ = "workspace_usage_events"
    __table_args__ = (
        UniqueConstraint("job_id", "event", name="uq_workspace_usage_event"),
        CheckConstraint("event IN ('reserve', 'consume', 'release')", name="ck_workspace_usage_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quota_id: Mapped[str] = mapped_column(ForeignKey("workspace_quotas.id", ondelete="RESTRICT"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("dmrv_jobs.id", ondelete="RESTRICT"), index=True)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    area_ha: Mapped[float] = mapped_column(Float, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
