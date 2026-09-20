"""Standalone, slug-addressable wildfire read API."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
from app.db.models.disaster_event import DisasterEvent
from app.db.models.hotspot import Hotspot
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.db.session import get_db
from app.services import firms_service, wildfire_hotspot_service

router = APIRouter(prefix="/disasters/wildfires", tags=["wildfires"])
MAX_MAP_FEATURES = 5000


def _map_features(features: list[dict]) -> tuple[list[dict], bool]:
    """Keep map payloads responsive without reducing stored/summary data."""
    if len(features) <= MAX_MAP_FEATURES:
        return features, False
    step = max(1, len(features) // MAX_MAP_FEATURES)
    sampled = features[::step][:MAX_MAP_FEATURES]
    return sampled, True


def _public_status(event: DisasterEvent) -> str:
    return (
        "active"
        if event.status == "published"
        else "archived"
        if event.status == "archived"
        else "monitoring"
    )


def _event(db: Session, slug: str) -> DisasterEvent:
    event = db.execute(
        select(DisasterEvent).where(
            DisasterEvent.slug == slug,
            DisasterEvent.status == "published",
            DisasterEvent.disaster_type == "forest_fire",
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Wildfire event not found")
    return event


def _summary(hotspots: list[Hotspot], event: DisasterEvent) -> dict:
    records = [h.stats or {} for h in hotspots]
    confidence = [
        str(record.get("confidence_category", record.get("confidence_label", "nominal"))).lower()
        for record in records
    ]
    frps = [float(record["frp"]) for record in records if record.get("frp") is not None]
    return {
        "total_hotspots": len(hotspots),
        "high_confidence_hotspots": confidence.count("high"),
        "nominal_confidence_hotspots": confidence.count("nominal"),
        "low_confidence_hotspots": confidence.count("low"),
        "affected_regions": len(event.province_codes or event.province or []),
        "total_frp": sum(frps) if frps else None,
        "average_frp": sum(frps) / len(frps) if frps else None,
        "latest_acquisition_time": max(
            (record.get("acq_datetime_utc") for record in records if record.get("acq_datetime_utc")),
            default=event.last_data_at.isoformat() if event.last_data_at else None,
        ),
        "burned_area_ha": getattr(event, "burned_area_ha", None),
        "burned_area_source": None,
        "previous_period_change_pct": None,
    }


def _feature(hotspot: Hotspot) -> dict | None:
    geojson = hotspot.geojson
    if not isinstance(geojson, dict):
        return None
    geometry = geojson.get("geometry", geojson)
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    properties = dict(hotspot.stats or {})
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "source": properties.get("source", "curated"),
            "source_label": properties.get("source_label", "Curated hotspot"),
            "latitude": properties.get("latitude"),
            "longitude": properties.get("longitude"),
            "acq_datetime_utc": properties.get("acq_datetime_utc"),
            "acq_date": properties.get("acq_date"),
            "acq_time": properties.get("acq_time", ""),
            "satellite": properties.get("satellite"),
            "instrument": properties.get("instrument"),
            "confidence": properties.get("confidence"),
            "confidence_numeric": properties.get("confidence_numeric"),
            "confidence_label": properties.get(
                "confidence_category", properties.get("confidence_label", "nominal")
            ),
            "frp": properties.get("frp"),
            "bright_ti4": properties.get("bright_ti4"),
            "bright_ti5": properties.get("bright_ti5"),
            "scan": properties.get("scan"),
            "track": properties.get("track"),
            "daynight": properties.get("daynight"),
            "resolution_m": properties.get("resolution_m", 0),
            "is_near_real_time": True,
        },
    }


def _period_dates(
    event: DisasterEvent, from_date: str | None, to: str | None
) -> tuple[str, str]:
    end_date = (
        to
        or (event.last_data_at.date().isoformat() if event.last_data_at else None)
        or (event.monitoring_to.isoformat() if event.monitoring_to else None)
        or dt.datetime.now(dt.UTC).date().isoformat()
    )
    start_date = (
        from_date
        or (event.monitoring_from.isoformat() if event.monitoring_from else None)
        or (event.start_date.isoformat() if event.start_date else None)
        or (dt.date.fromisoformat(end_date) - dt.timedelta(days=6)).isoformat()
    )
    return start_date, end_date


def _confidence_categories(confidence: str | None) -> set[str] | None:
    if confidence is None:
        return None
    selected = {item.strip().casefold() for item in confidence.split(",") if item.strip()}
    selected.discard("all")
    return selected


def _stored_wildfire_data(
    db: Session,
    event: DisasterEvent,
    from_date: str | None,
    to: str | None,
    sensor: str | None = None,
    confidence: str | None = None,
) -> tuple[list[dict], dict, dict]:
    """Read the persisted cache, initializing it once when necessary."""
    try:
        wildfire_hotspot_service.ensure_initial_sync(db, event)
    except firms_service.FirmsRequestError:
        # Keep the public endpoint useful when NASA is temporarily unavailable;
        # an already-populated database cache remains the source of truth.
        pass

    start_date, end_date = _period_dates(event, from_date, to)
    normalized_sensor = None if not sensor or sensor.casefold() == "all" else sensor
    features = wildfire_hotspot_service.list_event_features(
        db,
        event.id,
        start_date,
        end_date,
        sensor=normalized_sensor,
        confidence_categories=_confidence_categories(confidence),
    )
    # A populated cache can validly return no rows for a sensor/confidence
    # filter, so don't fall back to legacy curated rows in that case.
    has_persisted_rows = wildfire_hotspot_service.has_event_rows(db, event.id)

    if has_persisted_rows:
        summary = wildfire_hotspot_service.summarize(features)
        summary.update(
            {
                "affected_regions": len(event.province_codes or event.province or []),
                "burned_area_ha": None,
                "burned_area_source": None,
                "previous_period_change_pct": None,
            }
        )
        synced_at = event.hotspot_last_synced_at or event.last_synced_at
        metadata = {
            "source": "NASA FIRMS",
            "storage": "database",
            "fetched_at": synced_at.isoformat() if synced_at else None,
            "last_synced_at": synced_at.isoformat() if synced_at else None,
            "stale": False,
            "attribution": "Data: NASA FIRMS / NASA EOSDIS LANCE",
            "disclaimer": "Hotspot adalah indikasi anomali termal dan bukan bukti tunggal area terbakar.",
        }
        return features, summary, metadata

    # Backward-compatible fallback for legacy admin-curated rows created before
    # the persisted NASA observation table was introduced.
    hotspots = list(
        db.execute(
            select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True))
        ).scalars()
    )
    return (
        [feature for hotspot in hotspots if (feature := _feature(hotspot))],
        _summary(hotspots, event),
        {
            "source": "curated",
            "storage": "database",
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
            "last_synced_at": None,
            "stale": False,
            "attribution": event.source or "SaveGeo",
            "disclaimer": "Hotspot adalah indikasi anomali termal dan bukan bukti tunggal area terbakar.",
        },
    )


@router.get("/events")
def list_wildfire_events(
    search: str | None = None,
    year: int | None = None,
    status: str | None = None,
    province: str | None = None,
    severity: str | None = None,
    sort: str = "latest",
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    stmt = select(DisasterEvent).where(
        DisasterEvent.status == "published", DisasterEvent.disaster_type == "forest_fire"
    )
    if year:
        stmt = stmt.where(DisasterEvent.year == year)
    if severity:
        stmt = stmt.where(DisasterEvent.severity == severity)
    events = list(db.execute(stmt).scalars())
    if search:
        events = [event for event in events if search.lower() in event.name.lower()]
    if province:
        events = [event for event in events if province in (event.province or [])]
    items = []
    for event in events:
        count = len(
            db.execute(
                select(WildfireHotspot).where(
                    WildfireHotspot.event_id == event.id,
                    WildfireHotspot.is_published.is_(True),
                )
            ).scalars().all()
        )
        if count == 0:
            count = len(
                db.execute(
                    select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True))
                )
                .scalars()
                .all()
            )
        data = event.to_dict()
        data.update(
            {
                "title": data["name"],
                "provinces": data.get("province", []),
                "status": _public_status(event),
                "hotspot_count": count,
                "high_confidence_count": 0,
                "burned_area_ha": None,
                "burned_area_source": None,
            }
        )
        items.append(data)
    if sort == "hotspots":
        items.sort(key=lambda item: item["hotspot_count"], reverse=True)
    else:
        items.sort(key=lambda item: str(item.get("updated_at")), reverse=True)
    return {
        "events": items,
        "updated_at": max((item.get("updated_at") for item in items), default=None),
        "total": len(items),
    }


@router.get("/events/{slug}")
def get_wildfire_event(slug: str, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    event = _event(db, slug)
    data = event.to_dict()
    data["status"] = _public_status(event)
    data["title"] = data["name"]
    data["provinces"] = data.get("province", [])
    return {"event": data}


@router.get("/events/{slug}/summary")
def get_wildfire_summary(
    slug: str,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    confidence: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    _, summary, _ = _stored_wildfire_data(db, event, from_date, to, confidence=confidence)
    return summary


@router.get("/events/{slug}/hotspots")
def get_wildfire_hotspots(
    slug: str,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    sensor: str | None = None,
    confidence: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    features, summary, metadata = _stored_wildfire_data(db, event, from_date, to, sensor, confidence)
    map_features, truncated = _map_features(features)
    metadata = {
        **metadata,
        "total_features": len(features),
        "returned_features": len(map_features),
        "truncated_for_map": truncated,
    }
    return {
        "features": map_features,
        "summary": summary,
        "timeline": summary.get("by_day", []),
        "metadata": metadata,
    }


@router.get("/events/{slug}/timeline")
def get_wildfire_timeline(
    slug: str,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    sensor: str | None = None,
    confidence: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    _, summary, _ = _stored_wildfire_data(db, event, from_date, to, sensor, confidence)
    return {"timeline": summary.get("by_day", [])}


@router.get("/events/{slug}/regions")
def get_wildfire_regions(
    slug: str, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)
):
    event = _event(db, slug)
    return {
        "provinces": [
            {"code": code, "name": name, "hotspot_count": None}
            for code, name in zip(event.province_codes or [], event.province or [])
        ],
        "cities": [],
    }


@router.get("/events/{slug}/burned-area")
def get_wildfire_burned_area(
    slug: str, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)
):
    _event(db, slug)
    return {
        "available": False,
        "area_ha": None,
        "source": None,
        "period": None,
        "publication_date": None,
        "coverage": None,
        "unit": "ha",
        "note": "Tidak ada produk luas terbakar yang terpasang untuk event ini.",
    }
