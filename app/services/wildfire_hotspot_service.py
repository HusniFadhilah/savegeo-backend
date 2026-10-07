"""Database-backed storage and synchronization for NASA FIRMS observations."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from collections.abc import Iterable

from sqlalchemy import Date, Float, cast, func, select
from sqlalchemy.orm import Session

from app.db.models.disaster_event import DisasterEvent
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.services import firms_service
from app.services.wildfire_geometry import is_feature_in_event_scope

_INITIAL_SYNC_LOCK = threading.RLock()
# Start with FIRMS' five-day request window, then subdivide any response that
# reaches the server-side row limit so older observations are not discarded.
MAX_SYNC_SPAN_DAYS = 4
MAX_RETURNED_FEATURES = 50_000
MAX_SPATIAL_SPLIT_DEPTH = 6
EXISTING_ID_QUERY_BATCH_SIZE = 10_000


class IncompleteFirmsSyncError(firms_service.FirmsRequestError):
    """Raised when an upstream result remains truncated after subdivision."""


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


def _existing_rows_by_external_id(db: Session, event_id: int, external_ids: list[str]) -> dict[str, WildfireHotspot]:
    """Look up existing observations without exceeding PostgreSQL's bind limit."""
    existing: dict[str, WildfireHotspot] = {}
    for offset in range(0, len(external_ids), EXISTING_ID_QUERY_BATCH_SIZE):
        id_batch = external_ids[offset : offset + EXISTING_ID_QUERY_BATCH_SIZE]
        rows = db.execute(
            select(WildfireHotspot).where(
                WildfireHotspot.event_id == event_id,
                WildfireHotspot.external_id.in_(id_batch),
            )
        ).scalars()
        existing.update({row.external_id: row for row in rows})
    return existing


def _split_date_range(start: dt.date, end: dt.date) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    first_span = (end - start).days + 1
    midpoint = start + dt.timedelta(days=first_span // 2 - 1)
    return (start, midpoint), (midpoint + dt.timedelta(days=1), end)


def _split_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], ...]:
    west, south, east, north = bbox
    mid_lon = (west + east) / 2
    mid_lat = (south + north) / 2
    return (
        (west, south, mid_lon, mid_lat),
        (mid_lon, south, east, mid_lat),
        (west, mid_lat, mid_lon, north),
        (mid_lon, mid_lat, east, north),
    )


def _fetch_complete_period(
    source: str,
    start: dt.date,
    end: dt.date,
    bbox: tuple[float, float, float, float],
    spatial_depth: int = 0,
) -> list[dict]:
    """Fetch all rows by recursively splitting periods/areas that hit the API cap."""
    result = firms_service.get_fires_for_period(
        source=source,
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        bbox=bbox,
        confidence_categories=None,
        sensor=None,
        min_frp=None,
        limit=MAX_RETURNED_FEATURES,
    )
    if not result.get("metadata", {}).get("truncated", False):
        return result.get("features", [])

    if start < end:
        first, second = _split_date_range(start, end)
        return [
            *_fetch_complete_period(source, *first, bbox, spatial_depth),
            *_fetch_complete_period(source, *second, bbox, spatial_depth),
        ]

    if spatial_depth < MAX_SPATIAL_SPLIT_DEPTH:
        features: list[dict] = []
        for sub_bbox in _split_bbox(bbox):
            features.extend(_fetch_complete_period(source, start, end, sub_bbox, spatial_depth + 1))
        return features

    raise IncompleteFirmsSyncError(
        "NASA FIRMS masih mengembalikan data terpotong setelah pembagian area; sinkronisasi dibatalkan agar statistik tidak parsial.",
        502,
    )


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
        # Query each catalog source separately and recursively split saturated
        # results, avoiding partial counts during high-activity periods.
        for source in firms_service.FIRMS_SOURCE_CATALOG:
            try:
                fetched.extend(_fetch_complete_period(source, chunk_start, chunk_end, bbox))
            except IncompleteFirmsSyncError:
                raise
            except firms_service.FirmsRequestError as exc:
                if not fetched:
                    raise
                errors.append({"source": source, "message": str(exc), "status": exc.status_code})
                continue

    scoped_features = [feature for feature in fetched if is_feature_in_event_scope(event, feature)]
    features = _feature_map(scoped_features)
    external_ids = list(features)
    existing = _existing_rows_by_external_id(db, event.id, external_ids)

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
        "filtered_out_of_scope": len(fetched) - len(scoped_features),
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


def summarize_event(
    db: Session,
    event_id: int,
    from_date: dt.date | str,
    to_date: dt.date | str,
    sensor: str | None = None,
    confidence_categories: set[str] | None = None,
) -> dict:
    """Aggregate persisted observations in SQL without loading their geometries."""
    start = _date(from_date, dt.datetime.now(dt.UTC).date())
    end = _date(to_date, start)
    start_at = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
    end_at = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    confidence = func.lower(
        func.coalesce(
            WildfireHotspot.stats["confidence_label"].as_string(),
            WildfireHotspot.stats["confidence_category"].as_string(),
        )
    )
    frp = cast(WildfireHotspot.stats["frp"].as_string(), Float)
    acquired_at = WildfireHotspot.acquired_at
    date = cast(acquired_at, Date)
    stmt = (
        select(
            date.label("date"),
            WildfireHotspot.source.label("source"),
            confidence.label("confidence"),
            func.count().label("hotspot_count"),
            func.count(frp).label("frp_count"),
            func.sum(frp).label("frp_sum"),
            func.max(frp).label("frp_max"),
            func.max(acquired_at).label("latest"),
        )
        .where(
            WildfireHotspot.event_id == event_id,
            WildfireHotspot.is_published.is_(True),
            acquired_at >= start_at,
            acquired_at < end_at,
        )
    )
    normalized_sensor = sensor.casefold() if sensor else None
    if normalized_sensor:
        stmt = stmt.where(func.lower(WildfireHotspot.stats["instrument"].as_string()) == normalized_sensor)
    if confidence_categories is not None:
        stmt = stmt.where(confidence.in_(confidence_categories))
    rows = db.execute(
        stmt.group_by(date, WildfireHotspot.source, confidence)
    ).all()

    total = high = nominal = low = frp_count = 0
    frp_sum = 0.0
    frp_max = 0.0
    latest: dt.datetime | None = None
    by_day: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        count = int(row.hotspot_count or 0)
        total += count
        category = row.confidence
        if category == "high":
            high += count
        elif category == "nominal":
            nominal += count
        elif category == "low":
            low += count
        if row.date is not None:
            day = row.date.isoformat()
            by_day[day] = by_day.get(day, 0) + count
        source = str(row.source or "unknown")
        by_source[source] = by_source.get(source, 0) + count
        frp_count += int(row.frp_count or 0)
        frp_sum += float(row.frp_sum or 0)
        frp_max = max(frp_max, float(row.frp_max or 0))
        if row.latest is not None and (latest is None or row.latest > latest):
            latest = row.latest

    return {
        "total_hotspots": total,
        "high_confidence_hotspots": high,
        "nominal_confidence_hotspots": nominal,
        "low_confidence_hotspots": low,
        "total_frp": round(frp_sum, 3),
        "average_frp": round(frp_sum / frp_count, 3) if frp_count else None,
        "max_frp": round(frp_max, 3),
        "latest_detection_utc": latest.isoformat().replace("+00:00", "Z") if latest else None,
        "by_day": [{"date": day, "count": by_day[day]} for day in sorted(by_day)],
        "by_source": [{"source": source, "count": by_source[source]} for source in sorted(by_source)],
    }
