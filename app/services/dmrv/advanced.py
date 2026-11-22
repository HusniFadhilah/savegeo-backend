"""Transactional services for append-only credit events and field capture sync."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.dmrv_advanced import DmrvCreditEvent, DmrvFieldCapture
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_integrity import DmrvIntegrityDocument
from app.db.models.dmrv_field import SurveyPlot
from app.db.models.dmrv_project import DmrvProject
from app.schemas.dmrv_integrity import CreditEventInput, FieldCaptureInput
from app.services.dmrv import integrity
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError


def _distance_m(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    radians = math.pi / 180
    p1, p2 = lat_a * radians, lat_b * radians
    dp, dl = (lat_b - lat_a) * radians, (lon_b - lon_a) * radians
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(min(1, math.sqrt(a)))


def _same_credit(row: DmrvCreditEvent, data: CreditEventInput) -> bool:
    return all(
        getattr(row, field) == getattr(data, field)
        for field in (
            "batch_reference", "event_type", "quantity", "owner_from", "owner_to", "registry",
            "external_transaction_id", "effective_date", "evidence",
        )
    )


def _events(db: Session, project_id: str, batch: str) -> list[DmrvCreditEvent]:
    return db.execute(
        select(DmrvCreditEvent)
        .where(DmrvCreditEvent.project_id == project_id, DmrvCreditEvent.batch_reference == batch)
        .order_by(DmrvCreditEvent.created_at, DmrvCreditEvent.event_id)
    ).scalars().all()


def _balance(events: list[DmrvCreditEvent]) -> tuple[dict[str, int], str | None]:
    balances: dict[str, int] = {}
    registry = None
    for event in events:
        registry = registry or event.registry
        if event.event_type == "issuance_reported":
            if balances or not event.owner_to:
                raise DmrvError(409, "ledger_history_invalid", "Riwayat batch memiliki penerbitan yang bertentangan.")
            balances[event.owner_to] = event.quantity
        elif event.event_type == "transfer_reported":
            available = balances.get(event.owner_from or "", 0)
            if available < event.quantity or not event.owner_to:
                raise DmrvError(409, "ledger_history_invalid", "Saldo yang dilaporkan tidak cukup untuk transfer.")
            balances[event.owner_from or ""] = available - event.quantity
            balances[event.owner_to] = balances.get(event.owner_to, 0) + event.quantity
        else:
            available = balances.get(event.owner_from or "", 0)
            if available < event.quantity:
                raise DmrvError(409, "ledger_history_invalid", "Saldo yang dilaporkan tidak cukup untuk event ini.")
            balances[event.owner_from or ""] = available - event.quantity
    return balances, registry


def credit_event_out(row: DmrvCreditEvent) -> dict:
    return {
        "event_id": row.event_id,
        "batch_reference": row.batch_reference,
        "event_type": row.event_type,
        "quantity": row.quantity,
        "owner_from": row.owner_from,
        "owner_to": row.owner_to,
        "registry": row.registry,
        "external_transaction_id": row.external_transaction_id,
        "effective_date": row.effective_date.isoformat(),
        "evidence": row.evidence,
        "record_status": "reported_internal_only",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
    }


def list_credit_events(db: Session, principal: Principal, project_id: str) -> dict:
    authorize(db, project_id, principal, "accounting.read")
    rows = db.execute(
        select(DmrvCreditEvent).where(DmrvCreditEvent.project_id == project_id)
        .order_by(DmrvCreditEvent.created_at.desc())
    ).scalars().all()
    batches: dict[str, dict] = {}
    all_by_batch: dict[str, list[DmrvCreditEvent]] = {}
    for row in rows:
        all_by_batch.setdefault(row.batch_reference, []).append(row)
    for batch, batch_rows in all_by_batch.items():
        row = batch_rows[0]
        state = batches.setdefault(batch, {"balances": {}, "registry": row.registry})
        try:
            balance, registry = _balance(list(reversed(batch_rows)))
        except DmrvError:
            balance, registry = {}, row.registry
        state["balances"] = balance
        state["registry"] = registry
    return {
        "events": [credit_event_out(row) for row in rows],
        "batches": [
            {"batch_reference": batch, "registry": state["registry"],
             "reported_balances": [{"owner": owner, "quantity": quantity} for owner, quantity in state["balances"].items() if quantity]}
            for batch, state in batches.items()
        ],
        "qualification": "Internal event log of user-reported registry activity. It is not an official registry balance, issuance record, or synchronized feed.",
    }


def append_credit_event(db: Session, principal: Principal, project_id: str, data: CreditEventInput) -> dict:
    authorize(db, project_id, principal, "monitoring.write")
    integrity._documents_exist(db, project_id, data.evidence)
    existing_id = db.get(DmrvCreditEvent, data.event_id)
    if existing_id:
        if existing_id.project_id == project_id and _same_credit(existing_id, data):
            return credit_event_out(existing_id)
        raise DmrvError(409, "event_id_reused", "ID event sudah digunakan untuk data berbeda.")
    # Serialize event appends for this project so concurrent transfers cannot overspend.
    db.execute(select(DmrvProject.id).where(DmrvProject.id == project_id).with_for_update()).first()
    events = _events(db, project_id, data.batch_reference)
    balance, registry = _balance(events)
    if data.event_type == "issuance_reported":
        if events:
            raise DmrvError(409, "issuance_already_reported", "Batch ini sudah memiliki event; gunakan transfer atau event siklus berikutnya.")
    else:
        if not events:
            raise DmrvError(422, "issuance_required", "Catat penerbitan yang dilaporkan sebelum event lain untuk batch ini.")
        if data.effective_date < events[-1].effective_date:
            raise DmrvError(422, "event_date_out_of_order", "Tanggal event tidak boleh mendahului event batch terakhir.")
        if registry != data.registry:
            raise DmrvError(422, "registry_mismatch", "Event batch harus menggunakan registry yang sama.")
        if balance.get(data.owner_from or "", 0) < data.quantity:
            raise DmrvError(409, "reported_balance_insufficient", "Saldo internal yang dilaporkan tidak cukup.")
    row = DmrvCreditEvent(**data.model_dump(), project_id=project_id, created_by=principal.key)
    db.add(row)
    try:
        db.flush()
        record_audit(db, principal=principal, action="credit_event.report", resource_type="credit_event",
                     resource_id=row.event_id, project_id=project_id,
                     after={**credit_event_out(row), "evidence": row.evidence})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DmrvError(409, "duplicate_registry_transaction", "Event atau ID transaksi registry tersebut sudah dicatat.") from None
    except Exception:
        db.rollback()
        raise
    return credit_event_out(row)


def capture_out(row: DmrvFieldCapture) -> dict:
    return {
        "capture_id": row.capture_id,
        "project_id": row.project_id,
        "plot_code": row.plot_code,
        "plot_id": row.plot_id,
        "observed_at": row.observed_at.isoformat(),
        "latitude": row.latitude,
        "longitude": row.longitude,
        "gps_accuracy_m": row.gps_accuracy_m,
        "observation": row.observation,
        "photo_document_id": row.photo_document_id,
        "photo_url": f"/dmrv/projects/{row.project_id}/field-data/captures/{row.capture_id}/photo" if row.photo_document_id else None,
        "qa_flags": row.qa_flags or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
    }


def list_field_captures(
    db: Session, principal: Principal, project_id: str, *, limit: int | None = 500
) -> list[dict]:
    authorize(db, project_id, principal, "field.read")
    query = select(DmrvFieldCapture).where(DmrvFieldCapture.project_id == project_id).order_by(
        DmrvFieldCapture.observed_at.desc()
    )
    if limit is not None:
        query = query.limit(limit)
    rows = db.execute(query).scalars().all()
    return [capture_out(row) for row in rows]


def capture_field_observation(
    db: Session, principal: Principal, project_id: str, data: FieldCaptureInput,
    photo: bytes | None, filename: str | None,
) -> dict:
    authorize(db, project_id, principal, "field.write")
    digest = hashlib.sha256(photo).hexdigest() if photo else None
    existing = db.get(DmrvFieldCapture, data.capture_id)
    if existing:
        existing_doc = db.get(DmrvIntegrityDocument, existing.photo_document_id) if existing.photo_document_id else None
        same = (
            existing.project_id == project_id and existing.plot_code == data.plot_code
            and existing.observed_at == data.observed_at and existing.latitude == data.latitude
            and existing.longitude == data.longitude and existing.gps_accuracy_m == data.gps_accuracy_m
            and existing.observation == data.observation and (existing_doc.sha256 if existing_doc else None) == digest
        )
        if same:
            return capture_out(existing)
        raise DmrvError(409, "capture_id_reused", "ID catatan lapangan sudah digunakan untuk data berbeda.")

    boundary = db.execute(
        select(BoundaryVersion).where(BoundaryVersion.project_id == project_id)
        .order_by(BoundaryVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    qa_flags = []
    plot = db.execute(
        select(SurveyPlot).where(
            SurveyPlot.project_id == project_id, SurveyPlot.plot_code == data.plot_code
        )
    ).scalar_one_or_none()
    if plot is None:
        qa_flags.append("plot_code_not_in_project_register")
    elif plot.latitude is None or plot.longitude is None:
        qa_flags.append("plot_reference_coordinate_missing")
    else:
        offset_m = _distance_m(data.latitude, data.longitude, plot.latitude, plot.longitude)
        allowed_m = max(50.0, (data.gps_accuracy_m or 0) * 2)
        if offset_m > allowed_m:
            qa_flags.append("gps_offset_from_registered_plot_review")
    if data.gps_accuracy_m is None:
        qa_flags.append("gps_accuracy_not_recorded")
    elif data.gps_accuracy_m > 30:
        qa_flags.append("gps_accuracy_over_30m_review")
    if boundary:
        from shapely.geometry import Point, shape
        if not shape(boundary.validated_geojson).covers(Point(data.longitude, data.latitude)):
            qa_flags.append("coordinate_outside_current_boundary_review")
    else:
        qa_flags.append("project_boundary_not_available")
    if data.observed_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5):
        qa_flags.append("observation_timestamp_in_future_review")

    document = None
    path = None
    if photo is not None:
        if not photo or len(photo) > 5 * 1024 * 1024:
            raise DmrvError(413, "photo_size", "Foto harus berisi data dan maksimal 5 MB.")
        if photo.startswith(b"\x89PNG\r\n\x1a\n"):
            content_type, suffix = "image/png", ".png"
        elif photo.startswith(b"\xff\xd8\xff"):
            content_type, suffix = "image/jpeg", ".jpg"
        else:
            raise DmrvError(422, "photo_format", "Foto lapangan harus berupa PNG atau JPEG yang valid.")
        total = db.query(func.coalesce(func.sum(DmrvIntegrityDocument.size_bytes), 0)).filter(
            DmrvIntegrityDocument.project_id == project_id
        ).scalar() or 0
        if total + len(photo) > 50 * 1024 * 1024:
            raise DmrvError(413, "project_document_limit", "Batas dokumen proyek 50 MB tercapai.")
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (filename or "field-photo").rsplit(".", 1)[0])[:80]
        document = DmrvIntegrityDocument(
            id=str(uuid.uuid4()), project_id=project_id, filename=f"{safe_stem or 'field-photo'}{suffix}",
            content_type=content_type, sha256=digest, size_bytes=len(photo), created_by=principal.key,
        )
        path = integrity.document_path(document)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(photo)
        except OSError:
            path.unlink(missing_ok=True)
            raise DmrvError(500, "photo_storage_failed", "Foto gagal disimpan di server.") from None
        db.add(document)

    row = DmrvFieldCapture(
        **data.model_dump(), project_id=project_id,
        plot_id=plot.id if plot else None,
        photo_document_id=document.id if document else None, qa_flags=qa_flags, created_by=principal.key,
    )
    db.add(row)
    try:
        if document:
            db.flush()
        db.flush()
        record_audit(db, principal=principal, action="field.capture.sync", resource_type="field_capture",
                     resource_id=row.capture_id, project_id=project_id,
                     after={"plot_code": row.plot_code, "observed_at": row.observed_at.isoformat(),
                            "photo_sha256": digest, "qa_flags": qa_flags})
        db.commit()
    except IntegrityError:
        db.rollback()
        if path:
            path.unlink(missing_ok=True)
        raise DmrvError(409, "capture_conflict", "Catatan lapangan sudah tersinkron; muat ulang daftar.") from None
    except Exception:
        db.rollback()
        if path:
            path.unlink(missing_ok=True)
        raise
    return capture_out(row)
