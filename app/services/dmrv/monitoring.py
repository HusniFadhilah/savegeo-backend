"""Monitoring-period planning, versioning and lifecycle transitions."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.dmrv_audit import DmrvIdempotencyKey
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_project import DmrvProject
from app.schemas.dmrv import MonitoringPeriodCreate, MonitoringPeriodOut, MonitoringPeriodUpdate
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError


def _period_out(db: Session, row: DmrvMonitoringPeriod) -> MonitoringPeriodOut:
    boundary = db.get(BoundaryVersion, row.boundary_version_id)
    return MonitoringPeriodOut(
        id=row.id, project_id=row.project_id, boundary_version=boundary.version,
        boundary_checksum=boundary.checksum, name=row.name, start_date=row.start_date,
        end_date=row.end_date, status=row.status, revision=row.revision,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _boundary(db: Session, project_id: str, version: int) -> BoundaryVersion:
    row = db.execute(select(BoundaryVersion).where(
        BoundaryVersion.project_id == project_id, BoundaryVersion.version == version,
    )).scalar_one_or_none()
    if row is None:
        raise DmrvError(422, "boundary_version_not_found", "Versi batas tidak ditemukan pada proyek ini.")
    return row


def _lock_project(db: Session, project_id: str) -> None:
    # Serializes overlap checks for concurrent writes to the same project on PostgreSQL.
    db.execute(select(DmrvProject.id).where(DmrvProject.id == project_id).with_for_update()).scalar_one_or_none()


def _check_dates(db: Session, project_id: str, start: dt.date, end: dt.date, exclude_id: str | None = None) -> None:
    query = select(DmrvMonitoringPeriod.id).where(
        DmrvMonitoringPeriod.project_id == project_id,
        DmrvMonitoringPeriod.start_date <= end,
        DmrvMonitoringPeriod.end_date >= start,
    )
    if exclude_id is not None:
        query = query.where(DmrvMonitoringPeriod.id != exclude_id)
    if db.execute(query.limit(1)).first():
        raise DmrvError(409, "monitoring_period_overlap", "Periode monitoring tidak boleh tumpang tindih.")


def _request_hash(payload: MonitoringPeriodCreate) -> str:
    body = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _idempotency_replay(db: Session, principal: Principal, key: str, request_hash: str) -> MonitoringPeriodOut | None:
    row = db.execute(select(DmrvIdempotencyKey).where(
        DmrvIdempotencyKey.principal_key == principal.key, DmrvIdempotencyKey.key == key,
    )).scalar_one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash or row.response_body.get("kind") != "monitoring_period":
        raise DmrvError(422, "idempotency_key_reused", "Idempotency-Key sudah dipakai dengan isi permintaan berbeda.")
    return MonitoringPeriodOut.model_validate(row.response_body["body"])


def create_period(
    db: Session, principal: Principal, project_id: str, payload: MonitoringPeriodCreate,
    idempotency_key: str, request_id: str | None,
) -> tuple[MonitoringPeriodOut, bool]:
    authorize(db, project_id, principal, "monitoring.write")
    _lock_project(db, project_id)
    request_hash = _request_hash(payload)
    replay = _idempotency_replay(db, principal, idempotency_key, request_hash)
    if replay is not None:
        return replay, True
    boundary = _boundary(db, project_id, payload.boundary_version)
    _check_dates(db, project_id, payload.start_date, payload.end_date)
    row = DmrvMonitoringPeriod(
        id=str(uuid.uuid4()), project_id=project_id, boundary_version_id=boundary.id,
        name=payload.name.strip(), start_date=payload.start_date, end_date=payload.end_date,
        status="planned", revision=1, created_by=principal.key,
    )
    if not row.name:
        raise DmrvError(422, "invalid_period_name", "Nama periode wajib diisi.")
    try:
        db.add(row)
        db.flush()
        out = _period_out(db, row)
        record_audit(
            db, principal=principal, action="monitoring_period.create", resource_type="monitoring_period",
            resource_id=row.id, project_id=project_id,
            after={**out.model_dump(mode="json"), "kind": "monitoring_period"}, request_id=request_id,
        )
        db.add(DmrvIdempotencyKey(
            principal_key=principal.key, key=idempotency_key, request_hash=request_hash,
            response_status=201, response_body={"kind": "monitoring_period", "body": out.model_dump(mode="json")},
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = _idempotency_replay(db, principal, idempotency_key, request_hash)
        if replay is not None:
            return replay, True
        raise DmrvError(409, "monitoring_period_conflict", "Nama atau tanggal periode berbenturan dengan data yang sudah ada.") from None
    except Exception:
        db.rollback()
        raise
    return out, False


def list_periods(db: Session, principal: Principal, project_id: str) -> list[MonitoringPeriodOut]:
    authorize(db, project_id, principal, "monitoring.read")
    rows = db.execute(select(DmrvMonitoringPeriod).where(
        DmrvMonitoringPeriod.project_id == project_id,
    ).order_by(DmrvMonitoringPeriod.start_date, DmrvMonitoringPeriod.name)).scalars().all()
    return [_period_out(db, row) for row in rows]


def get_period(db: Session, principal: Principal, project_id: str, period_id: str) -> MonitoringPeriodOut:
    authorize(db, project_id, principal, "monitoring.read")
    row = db.execute(select(DmrvMonitoringPeriod).where(
        DmrvMonitoringPeriod.project_id == project_id, DmrvMonitoringPeriod.id == period_id,
    )).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Periode monitoring tidak ditemukan.")
    return _period_out(db, row)


def update_period(
    db: Session, principal: Principal, project_id: str, period_id: str,
    payload: MonitoringPeriodUpdate, if_match_revision: int, request_id: str | None,
) -> MonitoringPeriodOut:
    authorize(db, project_id, principal, "monitoring.write")
    _lock_project(db, project_id)
    row = db.execute(select(DmrvMonitoringPeriod).where(
        DmrvMonitoringPeriod.project_id == project_id, DmrvMonitoringPeriod.id == period_id,
    )).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Periode monitoring tidak ditemukan.")
    if row.revision != if_match_revision:
        raise DmrvError(412, "revision_mismatch", "Periode monitoring berubah sejak dimuat; muat ulang.")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return _period_out(db, row)
    before = {"name": row.name, "start_date": row.start_date.isoformat(), "end_date": row.end_date.isoformat(),
              "status": row.status, "revision": row.revision}
    status = changes.pop("status", None)
    if row.status == "completed":
        raise DmrvError(409, "period_completed", "Periode selesai bersifat terkunci.")
    detail_changes = set(changes) - {"status"}
    if detail_changes and row.status != "planned":
        raise DmrvError(409, "period_in_progress", "Detail hanya dapat diubah sebelum periode dimulai.")
    new_start = changes.get("start_date", row.start_date)
    new_end = changes.get("end_date", row.end_date)
    if new_end < new_start:
        raise DmrvError(422, "invalid_date_range", "end_date harus sama dengan atau setelah start_date.")
    if "start_date" in changes or "end_date" in changes:
        _check_dates(db, project_id, new_start, new_end, exclude_id=period_id)
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise DmrvError(422, "invalid_period_name", "Nama periode wajib diisi.")
    if "boundary_version" in changes:
        boundary = _boundary(db, project_id, changes.pop("boundary_version"))
        changes["boundary_version_id"] = boundary.id
    if status is not None:
        allowed = {"planned": {"in_progress"}, "in_progress": {"completed"}}
        if status not in allowed.get(row.status, set()):
            raise DmrvError(409, "invalid_period_transition", "Status periode hanya dapat bergerak planned → in_progress → completed.")
        changes["status"] = status
    values = {**changes, "revision": if_match_revision + 1, "updated_at": dt.datetime.now(dt.UTC)}
    try:
        result = db.execute(update(DmrvMonitoringPeriod).where(
            DmrvMonitoringPeriod.id == period_id,
            DmrvMonitoringPeriod.project_id == project_id,
            DmrvMonitoringPeriod.revision == if_match_revision,
        ).values(**values))
        if result.rowcount != 1:
            raise DmrvError(412, "revision_mismatch", "Periode monitoring berubah sejak dimuat; muat ulang.")
        db.flush()
        db.expire_all()
        updated = db.get(DmrvMonitoringPeriod, period_id)
        out = _period_out(db, updated)
        record_audit(
            db, principal=principal, action="monitoring_period.update", resource_type="monitoring_period",
            resource_id=period_id, project_id=project_id, before=before,
            after={**out.model_dump(mode="json"), "kind": "monitoring_period"}, request_id=request_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return out
