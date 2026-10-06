"""Standalone, slug-addressable wildfire read API."""

from __future__ import annotations

import datetime as dt
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
import app.api.routes.regions as regions_route
from app.db.models.disaster_event import DisasterEvent
from app.db.models.hotspot import Hotspot
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.db.session import get_db
from app.registries import disaster_model_registry
from app.repositories import disaster_repo
from app.services import disaster_capability_service, firms_service, wildfire_hotspot_service, wildfire_region_service

from app.core.viewer_features import require_feature

router = APIRouter(
    prefix="/disasters/wildfires",
    tags=["wildfires"],
    dependencies=[Depends(require_feature("app.feature.disaster_mapping"))],
)
MAX_MAP_FEATURES = 5000
ADMIN_REGION_LEVELS = {"province", "city", "district", "village"}
_ADMIN_GEOMETRY_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_ADMIN_GEOMETRY_CACHE_LOCK = threading.RLock()
_ADMIN_GEOMETRY_CACHE_TTL = 86400


def _admin_geometry(level: str, code: str) -> dict:
    key = (level, code)
    with _ADMIN_GEOMETRY_CACHE_LOCK:
        cached = _ADMIN_GEOMETRY_CACHE.get(key)
        if cached and time.time() - cached[0] <= _ADMIN_GEOMETRY_CACHE_TTL:
            return cached[1]
    geometry = (
        regions_route.get_province_geometry(code)
        if level == "province"
        else regions_route.get_region_geometry(endpoint=level, code=code)
    )
    with _ADMIN_GEOMETRY_CACHE_LOCK:
        _ADMIN_GEOMETRY_CACHE[key] = (time.time(), geometry)
    return geometry


def _filter_by_admin_region(
    event: DisasterEvent,
    features: list[dict],
    region_level: str | None,
    region_code: str | None,
) -> list[dict]:
    if not region_level and not region_code:
        return features
    if region_level not in ADMIN_REGION_LEVELS or not region_code:
        raise HTTPException(status_code=400, detail="region_level dan region_code harus diisi")
    if region_level == "province" and region_code not in (event.province_codes or []):
        raise HTTPException(status_code=400, detail="Provinsi berada di luar cakupan event")
    try:
        return wildfire_region_service.filter_features_by_boundary(
            features, _admin_geometry(region_level, region_code)
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Hotspot tidak dapat difilter berdasarkan batas wilayah") from exc


def _as_region_features(geometry: dict, name: str, code: str, level: str) -> list[dict]:
    if geometry.get("type") == "FeatureCollection":
        features = list(geometry.get("features") or [])
    elif geometry.get("type") == "Feature":
        features = [geometry]
    else:
        features = [{"type": "Feature", "geometry": geometry, "properties": {}}]
    for feature in features:
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            properties = {}
            feature["properties"] = properties
        properties.update({"name": name, "code": code, "level": level})
    return features


def _admin_child_features(event: DisasterEvent, level: str, parent_code: str | None) -> list[dict]:
    if level == "province":
        if parent_code:
            raise HTTPException(status_code=400, detail="Provinsi tidak memiliki wilayah induk")
        codes = event.province_codes or []
        names = event.province or []
        if not codes:
            raise HTTPException(status_code=422, detail="Kode provinsi event belum tersedia")
        def fetch_province(index_code: tuple[int, str]):
            index, code = index_code
            name = names[index] if index < len(names) else code
            return _as_region_features(_admin_geometry("province", code), name, code, level)
        with ThreadPoolExecutor(max_workers=min(5, len(codes))) as executor:
            return [feature for batch in executor.map(fetch_province, enumerate(codes)) for feature in batch]

    if not parent_code:
        raise HTTPException(status_code=400, detail="Pilih wilayah induk terlebih dahulu")
    parent_level = {"city": "province", "district": "city", "village": "district"}.get(level)
    if parent_level is None:
        raise HTTPException(status_code=400, detail="Tingkat wilayah tidak valid")
    if level == "city" and parent_code not in (event.province_codes or []):
        raise HTTPException(status_code=400, detail="Provinsi berada di luar cakupan event")
    result = regions_route.get_region_children_geometries(
        parent_code=parent_code,
        child_endpoint=level,
        parent_endpoint=parent_level,
        limit=10000,
    )
    features = result.get("features", []) if isinstance(result, dict) else []
    if not features:
        raise HTTPException(status_code=502, detail="Batas wilayah turunan tidak tersedia")
    return features


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


def _published_burned_area(db: Session, event: DisasterEvent) -> dict | None:
    """Read only a compatible, published burned-area AnalysisResult.

    Hotspot observations and a legacy event field are deliberately not used as
    an area fallback. Missing or stale analysis remains ``None`` instead of
    being presented as a precise-looking estimate.
    """
    model = disaster_model_registry.get_model("fire_burned_area_v1")
    if not model or not model.get("enabled"):
        return None
    for run, result in disaster_repo.list_published_analyses(db, event.id):
        if run.model_id != "fire_burned_area_v1":
            continue
        capability = disaster_capability_service.validate_persisted_result(db, event, run, result)
        if not capability.allowed:
            continue
        statistics = result.statistics or {}
        area_ha = statistics.get("area_ha")
        if not isinstance(area_ha, (int, float)) or not math.isfinite(float(area_ha)) or area_ha < 0:
            continue
        published_at = getattr(result, "published_at", None) or getattr(run, "completed_at", None)
        return {
            "area_ha": round(float(area_ha), 2),
            "source": statistics.get("product") or statistics.get("primary_source_id"),
            "dataset_id": statistics.get("dataset_id"),
            "period": statistics.get("period"),
            "coverage": statistics.get("coverage"),
            "unit": statistics.get("unit", "ha"),
            "publication_date": published_at.isoformat() if published_at else None,
            "resolution_m": statistics.get("resolution_m"),
            "area_method": statistics.get("area_method"),
        }
    return None


def _burned_area_summary_fields(area: dict | None) -> dict:
    return {
        "burned_area_ha": area.get("area_ha") if area else None,
        "burned_area_source": area.get("source") if area else None,
    }


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
        burned_area = _published_burned_area(db, event)
        summary.update(
            {
                "affected_regions": len(event.province_codes or event.province or []),
                **_burned_area_summary_fields(burned_area),
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
    burned_area = _published_burned_area(db, event)
    summary = _summary(hotspots, event)
    summary.update(_burned_area_summary_fields(burned_area))
    return (
        [feature for hotspot in hotspots if (feature := _feature(hotspot))],
        summary,
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
        try:
            wildfire_hotspot_service.ensure_initial_sync(db, event)
        except firms_service.FirmsRequestError:
            # The persisted cache remains readable when NASA FIRMS is
            # temporarily unavailable.
            pass
        persisted_stats = list(
            db.execute(
                select(WildfireHotspot.stats).where(
                    WildfireHotspot.event_id == event.id,
                    WildfireHotspot.is_published.is_(True),
                )
            ).scalars()
        )
        count = len(persisted_stats)
        high_confidence_count = sum(
            str((stats or {}).get("confidence_category", (stats or {}).get("confidence_label", ""))).casefold()
            == "high"
            for stats in persisted_stats
        )
        if count == 0:
            curated_rows = db.execute(
                select(Hotspot).where(Hotspot.event_id == event.id, Hotspot.is_published.is_(True))
            )
            count = len(curated_rows.scalars().all())
        data = event.to_dict()
        burned_area = _published_burned_area(db, event)
        data.update(
            {
                "title": data["name"],
                "provinces": data.get("province", []),
                "status": _public_status(event),
                "hotspot_count": count,
                "high_confidence_count": high_confidence_count,
                **_burned_area_summary_fields(burned_area),
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
    burned_area = _published_burned_area(db, event)
    data["status"] = _public_status(event)
    data["title"] = data["name"]
    data["provinces"] = data.get("province", [])
    data.update(_burned_area_summary_fields(burned_area))
    return {"event": data}


@router.get("/events/{slug}/summary")
def get_wildfire_summary(
    slug: str,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    confidence: str | None = None,
    region_level: str | None = None,
    region_code: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    features, summary, _ = _stored_wildfire_data(db, event, from_date, to, confidence=confidence)
    if region_level or region_code:
        features = _filter_by_admin_region(event, features, region_level, region_code)
        summary = wildfire_hotspot_service.summarize(features)
        summary.update({"affected_regions": 1, "burned_area_ha": None, "burned_area_source": None})
    return summary


@router.get("/events/{slug}/hotspots")
def get_wildfire_hotspots(
    slug: str,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    sensor: str | None = None,
    confidence: str | None = None,
    region_level: str | None = None,
    region_code: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    features, summary, metadata = _stored_wildfire_data(db, event, from_date, to, sensor, confidence)
    if region_level or region_code:
        features = _filter_by_admin_region(event, features, region_level, region_code)
        summary = wildfire_hotspot_service.summarize(features)
        summary.update({"affected_regions": 1, "burned_area_ha": None, "burned_area_source": None})
        metadata = {**metadata, "region_level": region_level, "region_code": region_code}
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


@router.get("/events/{slug}/region-statistics")
def get_wildfire_region_statistics(
    slug: str,
    level: str = Query(..., pattern="^(province|city|district|village)$"),
    parent_code: str | None = None,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    sensor: str | None = None,
    confidence: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    features, _, _ = _stored_wildfire_data(db, event, from_date, to, sensor, confidence)
    try:
        regions = _admin_child_features(event, level, parent_code)
        statistics = wildfire_region_service.summarize_by_region(features, regions)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Statistik hotspot per wilayah tidak dapat dimuat") from exc
    return {
        "level": level,
        "parent_code": parent_code,
        "total_hotspots": len(features),
        "regions": statistics,
        "attribution": "NASA FIRMS / NASA EOSDIS LANCE; batas administratif SP3STAB",
    }


@router.get("/events/{slug}/timeline")
def get_wildfire_timeline(
    slug: str,
    from_date: str | None = Query(None, alias="from"),
    to: str | None = None,
    sensor: str | None = None,
    confidence: str | None = None,
    region_level: str | None = None,
    region_code: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    event = _event(db, slug)
    features, summary, _ = _stored_wildfire_data(db, event, from_date, to, sensor, confidence)
    if region_level or region_code:
        features = _filter_by_admin_region(event, features, region_level, region_code)
        summary = wildfire_hotspot_service.summarize(features)
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
    event = _event(db, slug)
    area = _published_burned_area(db, event)
    if area:
        return {"available": True, **area, "note": "Luas hasil produk burned-area terpublikasi; bukan jumlah hotspot."}
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
