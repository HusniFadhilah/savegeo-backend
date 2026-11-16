"""Quota mutations only flush: enqueue/finalization owns the transaction.

Lock order: organization -> quota on reserve, job -> quota on settlement.
Settlement never locks organization, so it cannot invert the lock order.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.organization import Organization
from app.db.models.workspace_analysis import WorkspaceQuota, WorkspaceUsageEvent
from app.services.dmrv.errors import DmrvError


def cycle(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now(dt.UTC)).date().replace(day=1)


def quota(db: Session, organization_id: str, now: dt.datetime | None = None) -> WorkspaceQuota:
    # Serializes initial row creation as well as concurrent reservations.
    db.execute(select(Organization).where(Organization.id == organization_id).with_for_update()).scalar_one()
    row = db.execute(select(WorkspaceQuota).where(
        WorkspaceQuota.organization_id == organization_id, WorkspaceQuota.cycle_start == cycle(now),
    ).with_for_update()).scalar_one_or_none()
    if row is None:
        row = WorkspaceQuota(id=str(uuid.uuid4()), organization_id=organization_id, cycle_start=cycle(now),
                             run_limit=max(0, int(os.environ.get("WORKSPACE_MONTHLY_RUN_LIMIT", "100"))), reserved=0, used=0)
        db.add(row)
        db.flush()
    return row


def reserve(db: Session, organization_id: str, job_id: str, kind: str, area_ha: float, now: dt.datetime | None = None) -> None:
    row = quota(db, organization_id, now)
    existing = db.scalar(select(WorkspaceUsageEvent.id).where(
        WorkspaceUsageEvent.job_id == job_id, WorkspaceUsageEvent.event == "reserve"))
    if existing:
        return
    if row.reserved + row.used >= row.run_limit:
        raise DmrvError(429, "quota_exhausted", "Kuota pekerjaan analisis bulan ini telah habis.")
    row.reserved += 1
    db.add(WorkspaceUsageEvent(id=str(uuid.uuid4()), quota_id=row.id, job_id=job_id, event="reserve",
                              analysis_kind=kind, area_ha=area_ha, detail={"policy": "analysis_run_v1", "unit": "analysis_run"}))
    db.flush()


def settle(db: Session, job_id: str, succeeded: bool, now: dt.datetime | None = None) -> None:
    reservation = db.scalar(select(WorkspaceUsageEvent).where(
        WorkspaceUsageEvent.job_id == job_id, WorkspaceUsageEvent.event == "reserve"))
    if reservation is None:
        return
    row = db.execute(select(WorkspaceQuota).where(WorkspaceQuota.id == reservation.quota_id).with_for_update()).scalar_one()
    settled = db.scalar(select(WorkspaceUsageEvent.id).where(
        WorkspaceUsageEvent.job_id == job_id, WorkspaceUsageEvent.event.in_(["consume", "release"])))
    if settled:
        return
    row.reserved -= 1
    if succeeded:
        row.used += 1
    db.add(WorkspaceUsageEvent(id=str(uuid.uuid4()), quota_id=row.id, job_id=job_id,
                              event="consume" if succeeded else "release", analysis_kind=reservation.analysis_kind,
                              area_ha=reservation.area_ha, detail={"policy": row.policy_version},
                              created_at=now or dt.datetime.now(dt.UTC)))
    db.flush()


def summary(db: Session, organization_id: str) -> dict:
    row = quota(db, organization_id)
    result = {"cycle_start": row.cycle_start.isoformat(), "timezone": "UTC", "unit": "analysis_run",
              "limit": row.run_limit, "reserved": row.reserved, "used": row.used,
              "remaining": row.run_limit - row.reserved - row.used, "policy_version": row.policy_version}
    db.commit()
    return result
