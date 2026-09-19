"""Database-backed storage and synchronization for NASA FIRMS observations."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.disaster_event import DisasterEvent
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.services import firms_service

_INITIAL_SYNC_LOCK = threading.RLock()
MAX_SYNC_SPAN_DAYS = 31
MAX_RETURNED_FEATURES = 2000


def _utc_datetime(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif value:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


def _date(value: dt.date | str | None, fallback: dt.date) -> dt.date:
    if value is None:
        return fallback
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise firms_service.FirmsRequestError("Tanggal sinkronisasi harus berformat YYYY-MM-DD", 400) from exc


def _event_period(
    event: DisasterEvent,
    from_date: dt.date | str | None,
    to_date: dt.date | str | None,
) -> tuple[dt.date, dt.date]:
    today = dt.datetime.now(dt.UTC).date()
    start = _date(from_date, event.monitoring_from or event.start_date or today)
    end = _date(
        to_date,
        event.monitoring_to
        or (event.last_data_at.date() if event.last_data_at else None)
        or event.end_date
        or today,
    )
    if end < start:
        raise firms_service.FirmsRequestError("to tidak boleh lebih awal dari from", 400)
    return start, end


def _date_chunks(start: dt.date, end: dt.date) -> Iterable[tuple[dt.date, dt.date]]:
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + dt.timedelta(days=MAX_SYNC_SPAN_DAYS))
        yield cursor, chunk_end
        cursor = chunk_end + dt.timedelta(days=1)


def _external_id(feature: dict) -> str:
    properties = feature.get("properties") or {}
    source = str(properties.get("source") or "unknown")
    feature_id = str(feature.get("id") or "")
    if not feature_id:
        feature_id = hashlib.sha256(json.dumps(feature, sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"{source}:{feature_id}"[:180]


def _feature_map(features: Iterable[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            continue
        result[_external_id(feature)] = feature
    return result


def _event_bbox(event: DisasterEvent) -> tuple[float, float, float, float]:
    if not event.bbox or len(event.bbox) != 4:
        raise firms_service.FirmsRequestError("Event wildfire belum memiliki bounding box", 422)
    try:
        return tuple(float(value) for value in event.bbox)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise firms_service.FirmsRequestError("Bounding box event wildfire tidak valid", 422) from exc


def sync_event_hotspots(
    db: Session,
    event: DisasterEvent,
    from_date: dt.date | str | None = None,
    to_date: dt.date | str | None = None,
) -> dict:
    """Fetch the requested period once and upsert it into the database.

    The upstream request is intentionally confined to this explicit sync path;
    public reads never call NASA FIRMS directly.
    """
    start, end = _event_period(event, from_date, to_date)
    bbox = _event_bbox(event)
    fetched: list[dict] = []
    errors: list[dict] = []
    for chunk_start, chunk_end in _date_chunks(start, end):
        # Query each catalog source separately so the per-request FIRMS limit
        # cannot truncate a multi-sensor sync before the observations reach DB.
        for source in firms_service.FIRMS_SOURCE_CATALOG:
            try:
                result = firms_service.get_fires_for_period(
                    source=source,
                    from_date=chunk_start.isoformat(),
                    to_date=chunk_end.isoformat(),
                    bbox=bbox,
                    confidence_categories=None,
                    sensor=None,
                    min_frp=None,
                    limit=MAX_RETURNED_FEATURES,
                )
            except firms_service.FirmsRequestError as exc:
                if not fetched:
                    raise
                errors.append({"source": source, "message": str(exc), "status": exc.status_code})
                continue
            fetched.extend(result.get("features", []))
            errors.extend(result.get("metadata", {}).get("errors", []))

    features = _feature_map(fetched)
    external_ids = list(features)
    existing = {
        row.external_id: row
        for row in db.execute(
            select(WildfireHotspot).where(
                WildfireHotspot.event_id == event.id,
                WildfireHotspot.external_id.in_(external_ids),
            )
        ).scalars()
    } if external_ids else {}

    now = dt.datetime.now(dt.UTC)
    for external_id, feature in features.items():
        properties = dict(feature.get("properties") or {})
        geometry = dict(feature["geometry"])
        source = str(properties.get("source") or "NASA FIRMS")
        acquired_at = _utc_datetime(properties.get("acq_datetime_utc"))
        row = existing.get(external_id)
        if row is None:
            row = WildfireHotspot(
                event_id=event.id,
                external_id=external_id,
                source=source,
                acquired_at=acquired_at,
                geojson=geometry,
                stats=properties,
                is_published=True,
                synced_at=now,
            )
            db.add(row)
        else:
            row.source = source
            row.acquired_at = acquired_at
            row.geojson = geometry
            row.stats = properties
            row.is_published = True
            row.synced_at = now

    db.flush()
    latest = db.execute(
        select(WildfireHotspot.acquired_at)
        .where(WildfireHotspot.event_id == event.id, WildfireHotspot.acquired_at.is_not(None))
        .order_by(WildfireHotspot.acquired_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    event.hotspot_last_synced_at = now
    event.last_synced_at = now
    if latest is not None:
        event.last_data_at = latest
    db.commit()
    db.refresh(event)

    return {
        "event_id": event.id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "fetched": len(fetched),
        "stored": len(features),
        "updated_at": now.isoformat(),
        "errors": errors,
    }


def ensure_initial_sync(db: Session, event: DisasterEvent) -> dict | None:
    """Populate an event lazily exactly once when its cache is uninitialized."""
    if event.hotspot_last_synced_at is not None:
        return None
    with _INITIAL_SYNC_LOCK:
        db.refresh(event)
        if event.hotspot_last_synced_at is not None:
            return None
        return sync_event_hotspots(db, event)


def list_event_features(
    db: Session,
    event_id: int,
    from_date: dt.date | str,
    to_date: dt.date | str,
    sensor: str | None = None,
    confidence_categories: set[str] | None = None,
) -> list[dict]:
    start = _date(from_date, dt.datetime.now(dt.UTC).date())
    end = _date(to_date, start)
    start_at = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
    end_at = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    rows = db.execute(
        select(WildfireHotspot)
        .where(
            WildfireHotspot.event_id == event_id,
            WildfireHotspot.is_published.is_(True),
            WildfireHotspot.acquired_at >= start_at,
            WildfireHotspot.acquired_at < end_at,
        )
        .order_by(WildfireHotspot.acquired_at.desc())
    ).scalars()
    features = []
    normalized_sensor = sensor.casefold() if sensor else None
    for row in rows:
        feature = row.to_feature()
        properties = feature["properties"]
        if normalized_sensor and str(properties.get("instrument") or "").casefold() != normalized_sensor:
            continue
        if confidence_categories is not None and properties.get("confidence_label") not in confidence_categories:
            continue
        features.append(feature)
    return features


def has_event_rows(db: Session, event_id: int) -> bool:
    return (
        db.execute(
            select(WildfireHotspot.id)
            .where(WildfireHotspot.event_id == event_id, WildfireHotspot.is_published.is_(True))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def summarize(features: list[dict]) -> dict:
    return firms_service._summary(features)
