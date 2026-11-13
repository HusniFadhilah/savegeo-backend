"""Durable, boundary-pinned satellite screening and opt-in scheduling."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_integrity import DmrvIntegrityRecord, DmrvRiskPolicy
from app.db.models.dmrv_job import DmrvJob
from app.schemas.dmrv_integrity import RiskPolicyInput
from app.services.dmrv import jobs
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.integrity import canonical, policy_out, retain_revision

logger = logging.getLogger(__name__)


def save_policy(
    db: Session, principal: Principal, project_id: str, data: RiskPolicyInput, expected: int
) -> dict:
    authorize(db, project_id, principal, "monitoring.write")
    row = db.get(DmrvRiskPolicy, project_id)
    if (row.revision if row else 0) != expected:
        raise DmrvError(412, "revision_mismatch", "Jadwal berubah; muat ulang.")
    before = policy_out(row)
    values = data.model_dump()
    values.update(
        updated_by=principal.key,
        next_run_at=dt.datetime.now(dt.UTC) if data.enabled else None,
        revision=expected + 1,
    )
    try:
        if row is None:
            row = DmrvRiskPolicy(project_id=project_id, **values)
            db.add(row)
            db.flush()
        else:
            changed = db.execute(
                update(DmrvRiskPolicy)
                .where(DmrvRiskPolicy.project_id == project_id, DmrvRiskPolicy.revision == expected)
                .values(**values)
            )
            if changed.rowcount != 1:
                raise DmrvError(412, "revision_mismatch", "Jadwal berubah; muat ulang.")
            db.refresh(row)
        record_audit(
            db,
            principal=principal,
            action="integrity.risk_policy.change",
            resource_type="risk_policy",
            resource_id=project_id,
            project_id=project_id,
            before={k: str(v) if isinstance(v, dt.datetime) else v for k, v in before.items()},
            after={k: str(v) if isinstance(v, dt.datetime) else v for k, v in policy_out(row).items()},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DmrvError(409, "conflict", "Jadwal dibuat bersamaan; muat ulang.") from None
    except Exception:
        db.rollback()
        raise
    return policy_out(row)


def enqueue_scan(
    db: Session,
    principal: Principal,
    project_id: str,
    end_date: dt.date | None,
    key: str,
    *,
    commit: bool = True,
) -> DmrvJob:
    authorize(db, project_id, principal, "monitoring.write")
    end = end_date or dt.datetime.now(dt.UTC).date()
    if end > dt.datetime.now(dt.UTC).date() or end < dt.date(2016, 1, 1):
        raise DmrvError(422, "invalid_scan_date", "Tanggal scan harus antara 2016 dan hari ini.")
    boundary = db.execute(
        select(BoundaryVersion)
        .where(BoundaryVersion.project_id == project_id)
        .order_by(BoundaryVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if boundary is None:
        raise DmrvError(422, "boundary_required", "Simpan batas proyek sebelum menjalankan scan.")
    policy = policy_out(db.get(DmrvRiskPolicy, project_id))
    payload = {
        "boundary_id": boundary.id,
        "boundary_checksum": boundary.checksum,
        "end_date": end.isoformat(),
        "lookback_days": policy["lookback_days"],
        "tree_loss_threshold_ha": policy["tree_loss_threshold_ha"],
        "min_observations": policy["min_observations"],
    }
    digest = hashlib.sha256(canonical(payload)).hexdigest()
    existing = db.execute(
        select(DmrvJob).where(DmrvJob.created_by == principal.key, DmrvJob.idempotency_key == key)
    ).scalar_one_or_none()
    if existing:
        if existing.project_id != project_id or existing.request_hash != digest:
            raise DmrvError(409, "idempotency_key_reused", "Kunci sudah dipakai untuk scan berbeda.")
        return existing
    job = jobs.enqueue(
        db,
        project_id=project_id,
        kind="integrity_risk_scan",
        payload=payload,
        created_by=principal.key,
        idempotency_key=key,
        request_hash=digest,
    )
    record_audit(
        db,
        principal=principal,
        action="integrity.risk_scan.queue",
        resource_type="dmrv_job",
        resource_id=job.id,
        project_id=project_id,
        after=payload,
    )
    if commit:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise DmrvError(409, "conflict", "Scan dijadwalkan bersamaan; muat ulang.") from None
    return job


def enqueue_due(db: Session, now: dt.datetime | None = None) -> int:
    """Called by the existing durable worker; SKIP LOCKED prevents duplicate schedulers."""
    moment = now or dt.datetime.now(dt.UTC)
    rows = (
        db.execute(
            select(DmrvRiskPolicy)
            .where(DmrvRiskPolicy.enabled.is_(True), DmrvRiskPolicy.next_run_at <= moment)
            .order_by(DmrvRiskPolicy.next_run_at)
            .limit(25)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    queued = 0
    for row in rows:
        kind, ident = row.updated_by.split(":")
        principal = Principal(kind, int(ident))
        try:
            active = db.execute(
                select(DmrvJob.id).where(
                    DmrvJob.project_id == row.project_id,
                    DmrvJob.kind == "integrity_risk_scan",
                    DmrvJob.status.in_(["queued", "running"]),
                )
            ).first()
            if not active:
                key = f"risk-auto-{row.project_id}-{moment.date().isoformat()}"
                enqueue_scan(db, principal, row.project_id, moment.date(), key, commit=False)
                row.last_enqueued_at = moment
                queued += 1
        except DmrvError as exc:
            # Missing boundary or revoked writer role stays visible as a scheduling audit, never a successful scan.
            record_audit(
                db,
                principal=principal,
                action="integrity.risk_schedule.blocked",
                resource_type="risk_policy",
                resource_id=row.project_id,
                project_id=row.project_id,
                after={"code": exc.code},
            )
        row.next_run_at = moment + dt.timedelta(days=row.interval_days)
    db.commit()
    return queued


def earth_engine_signals(geometry: dict, payload: dict) -> dict:
    """Two equal windows; 30 m screening grid; probabilities are not measured forest loss."""
    import ee

    end = dt.date.fromisoformat(payload["end_date"]) + dt.timedelta(days=1)
    middle = end - dt.timedelta(days=payload["lookback_days"])
    start = middle - dt.timedelta(days=payload["lookback_days"])
    roi = ee.Geometry(geometry)
    dw = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterBounds(roi).select("trees")
    before = dw.filterDate(start.isoformat(), middle.isoformat())
    after = dw.filterDate(middle.isoformat(), end.isoformat())
    counts = ee.Dictionary({"before_scenes": before.size(), "after_scenes": after.size()}).getInfo()
    change_ha, support_ha = None, 0.0
    if counts["before_scenes"] and counts["after_scenes"]:
        support = (
            before.count()
            .gte(payload["min_observations"])
            .And(after.count().gte(payload["min_observations"]))
        )
        change = before.median().gte(0.6).And(after.median().lte(0.4)).And(support)
        areas = ee.Image.pixelArea().divide(10000)
        stats = (
            areas.updateMask(change)
            .rename("change_ha")
            .addBands(areas.updateMask(support).rename("support_ha"))
            .reduceRegion(reducer=ee.Reducer.sum(), geometry=roi, scale=30, maxPixels=1e9, tileScale=8)
            .getInfo()
        )
        change_ha, support_ha = float(stats.get("change_ha") or 0), float(stats.get("support_ha") or 0)
    fires = (
        ee.ImageCollection("FIRMS")
        .filterBounds(roi)
        .filterDate(middle.isoformat(), end.isoformat())
        .select("confidence")
    )
    fire_scenes = fires.size().getInfo()
    fire_cells = None
    if fire_scenes:
        fire_cells = (
            fires.max()
            .gte(80)
            .selfMask()
            .reduceRegion(reducer=ee.Reducer.count(), geometry=roi, scale=1000, maxPixels=1e7, tileScale=8)
            .getInfo()
            .get("confidence", 0)
            or 0
        )
    return {
        **counts,
        "potential_tree_cover_change_ha": change_ha,
        "supported_area_ha": support_ha,
        "high_confidence_fire_raster_cells": fire_cells,
        "fire_scenes": fire_scenes,
        "baseline_start": start.isoformat(),
        "baseline_end_exclusive": middle.isoformat(),
        "monitoring_start": middle.isoformat(),
        "monitoring_end_exclusive": end.isoformat(),
        "sources": ["GOOGLE/DYNAMICWORLD/V1", "FIRMS"],
        "screening_scale_m": 30,
        "fire_scale_m": 1000,
        "tree_probability_thresholds": [0.6, 0.4],
        "qualification": "Potential tree-cover change and thermal raster intersections require human review; no burned area or carbon loss is inferred.",
    }


def validate_scan_payload(payload: dict) -> dict:
    required = {
        "boundary_id",
        "boundary_checksum",
        "end_date",
        "lookback_days",
        "tree_loss_threshold_ha",
        "min_observations",
    }
    if set(payload) != required:
        raise ValueError("Invalid risk scan payload")
    return payload


def run_scan(ctx) -> dict:
    from app.services.dmrv.job_handlers import JobFailed, RetryableJobError
    from app.services.gee_service import initialize_ee

    authorize(ctx.db, ctx.project_id, ctx.principal, "monitoring.write")
    boundary = ctx.db.get(BoundaryVersion, ctx.payload["boundary_id"])
    if (
        boundary is None
        or boundary.project_id != ctx.project_id
        or boundary.checksum != ctx.payload["boundary_checksum"]
    ):
        raise JobFailed("boundary_changed", "Versi batas scan tidak cocok.")
    ctx.stage("satellite_screening")
    ctx.checkpoint()
    try:
        if not initialize_ee(ctx.control):
            raise RuntimeError("Earth Engine credentials unavailable")
        signals = earth_engine_signals(boundary.validated_geojson, ctx.payload)
    except Exception:
        logger.exception("Satellite integrity scan unavailable job=%s", ctx.job_id)
        raise RetryableJobError(
            "satellite_unavailable", "Scan satelit belum berhasil; periksa kredensial atau coba kembali."
        ) from None
    ctx.checkpoint()
    ctx.stage("saving_screening")
    moment = ctx.clock()
    digest = hashlib.sha256(canonical(ctx.payload)).hexdigest()[:24]
    existing = ctx.db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == ctx.project_id,
            DmrvIntegrityRecord.category == "scan",
            DmrvIntegrityRecord.record_key == digest,
        )
    ).scalar_one_or_none()
    if existing:
        return {"record_id": existing.id, "reused": True}
    events = []
    if signals["supported_area_ha"] < 0.5 * boundary.area_ha or signals["fire_scenes"] == 0:
        events.append(("data_gap", "medium", "Cakupan observasi satelit belum memadai"))
    if (signals["potential_tree_cover_change_ha"] or 0) >= ctx.payload["tree_loss_threshold_ha"]:
        events.append(("tree_cover_change", "high", "Indikasi perubahan tutupan pohon"))
    if (signals["high_confidence_fire_raster_cells"] or 0) > 0:
        events.append(("fire_detection", "high", "Indikasi termal FIRMS di batas proyek"))
    result = {
        **signals,
        "boundary_version": boundary.version,
        "boundary_checksum": boundary.checksum,
        "event_count": len(events),
        "job_id": ctx.job_id,
    }
    scan = DmrvIntegrityRecord(
        id=str(uuid.uuid4()),
        project_id=ctx.project_id,
        category="scan",
        record_key=digest,
        title="Pemeriksaan satelit",
        payload=ctx.payload,
        result=result,
        status="resolved",
        revision=1,
        created_by=ctx.created_by,
        updated_by=ctx.created_by,
        created_at=moment,
        updated_at=moment,
    )
    ctx.db.add(scan)
    ctx.db.flush()
    retain_revision(ctx.db, scan, ctx.principal)
    for signal, severity, title in events:
        row = DmrvIntegrityRecord(
            id=str(uuid.uuid4()),
            project_id=ctx.project_id,
            category="risk",
            record_key=f"{digest}-{signal}",
            title=title,
            payload={
                "signal": signal,
                "severity": severity,
                "summary": title,
                "evidence": [],
                "observed_on": ctx.payload["end_date"],
                "owner": "",
                "due_date": None,
                "affected_area_ha": signals["potential_tree_cover_change_ha"]
                if signal == "tree_cover_change"
                else None,
            },
            result={"scan_record_id": scan.id, **result},
            status="open",
            revision=1,
            created_by=ctx.created_by,
            updated_by=ctx.created_by,
            created_at=moment,
            updated_at=moment,
        )
        ctx.db.add(row)
        ctx.db.flush()
        retain_revision(ctx.db, row, ctx.principal)
    return {"record_id": scan.id, "event_count": len(events), "boundary_version": boundary.version}
