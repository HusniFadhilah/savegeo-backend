"""Standalone, slug-addressable wildfire read API."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
from app.db.models.disaster_event import DisasterEvent
from app.db.models.hotspot import Hotspot
from app.db.session import get_db
from app.services import firms_service

router = APIRouter(prefix="/disasters/wildfires", tags=["wildfires"])


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
            db.execute(select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True)))
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
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    hotspots = list(
        db.execute(
            select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True))
        ).scalars()
    )
    return _summary(hotspots, event)


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
    if event.bbox:
        try:
            selected_confidence = None if confidence is None else {
                item.strip().casefold() for item in confidence.split(",") if item.strip()
            }
            if selected_confidence is not None:
                selected_confidence -= {"all"}
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
            live = firms_service.get_fires_for_period(
                source="ALL",
                from_date=start_date,
                to_date=end_date,
                bbox=tuple(event.bbox),
                confidence_categories=selected_confidence,
                sensor=None if not sensor or sensor.casefold() == "all" else sensor,
                min_frp=None,
                limit=2000,
            )
            summary = live.get("metadata", {}).get("summary") or _summary([], event)
            return {
                "features": live.get("features", []),
                "summary": {
                    **summary,
                    "affected_regions": len(event.province_codes or event.province or []),
                    "burned_area_ha": None,
                    "burned_area_source": None,
                },
                "metadata": {
                    "source": "NASA FIRMS",
                    "fetched_at": live.get("metadata", {}).get(
                        "fetched_at", dt.datetime.now(dt.UTC).isoformat()
                    ),
                    "stale": live.get("metadata", {}).get("stale", False),
                    "attribution": live.get("metadata", {}).get("attribution", "NASA FIRMS"),
                    "disclaimer": live.get("metadata", {}).get(
                        "disclaimer",
                        "Hotspot adalah indikasi anomali termal dan bukan bukti tunggal area terbakar.",
                    ),
                },
            }
        except firms_service.FirmsRequestError:
            pass
    hotspots = list(
        db.execute(
            select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True))
        ).scalars()
    )
    return {
        "features": [feature for hotspot in hotspots if (feature := _feature(hotspot))],
        "summary": _summary(hotspots, event),
        "metadata": {
            "source": "curated",
            "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
            "stale": False,
            "attribution": event.source or "SaveGeo",
            "disclaimer": "Hotspot adalah indikasi anomali termal dan bukan bukti tunggal area terbakar.",
        },
    }


@router.get("/events/{slug}/timeline")
def get_wildfire_timeline(
    slug: str, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)
):
    event = _event(db, slug)
    hotspots = list(
        db.execute(
            select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True))
        ).scalars()
    )
    counts: dict[str, int] = {}
    for hotspot in hotspots:
        date = str((hotspot.stats or {}).get("acq_date", "unknown"))
        if date != "unknown":
            counts[date] = counts.get(date, 0) + 1
    return {"timeline": [{"date": date, "count": count} for date, count in sorted(counts.items())]}


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
