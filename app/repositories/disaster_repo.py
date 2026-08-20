"""Read/write helpers shared by both `admin_disaster.py` and `disaster.py`
(user) routers, so the two route modules never duplicate query logic that
has to agree on the publish boundary. The one rule every read helper here
respects: **User-facing queries always filter `is_published`/`status`
themselves** - callers on the user side must pass `published_only=True`.

Written first (ahead of the route modules) precisely so route implementation
work can proceed in parallel against a single, already-correct data-access
layer instead of two routers each growing their own queries.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.analysis_result import AnalysisResult
from app.db.models.analysis_run import AnalysisRun
from app.db.models.disaster_aoi import DisasterAOI
from app.db.models.disaster_event import DisasterEvent
from app.db.models.hotspot import Hotspot
from app.db.models.satellite_imagery import SatelliteImagery

# --- Disaster events ---------------------------------------------------


def create_event(db: Session, data: dict, created_by: int | None) -> DisasterEvent:
    event = DisasterEvent(
        name=data["name"],
        disaster_type=data["disaster_type"],
        location_name=data.get("location_name"),
        province=data.get("province") or [],
        district=data.get("district") or [],
        event_date=data.get("event_date"),
        start_date=data.get("start_date"),
        end_date=data.get("end_date"),
        status=data.get("status", "draft"),
        severity=data.get("severity"),
        description=data.get("description"),
        source=data.get("source"),
        thumbnail=data.get("thumbnail"),
        created_by=created_by,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def update_event(db: Session, event: DisasterEvent, data: dict) -> DisasterEvent:
    for field in (
        "name", "disaster_type", "location_name", "province", "district",
        "event_date", "start_date", "end_date", "status", "severity",
        "description", "source", "thumbnail",
    ):
        if field in data:
            setattr(event, field, data[field])
    db.commit()
    db.refresh(event)
    return event


def get_event(db: Session, event_id: int) -> DisasterEvent | None:
    return db.get(DisasterEvent, event_id)


def delete_event(db: Session, event: DisasterEvent) -> None:
    db.delete(event)
    db.commit()


def list_events(
    db: Session,
    *,
    published_only: bool,
    status: str | None = None,
    disaster_type: str | None = None,
    province: str | None = None,
    severity: str | None = None,
    year: int | None = None,
    search: str | None = None,
) -> list[DisasterEvent]:
    stmt = select(DisasterEvent)
    if published_only:
        stmt = stmt.where(DisasterEvent.status == "published")
    elif status:
        stmt = stmt.where(DisasterEvent.status == status)
    if disaster_type:
        stmt = stmt.where(DisasterEvent.disaster_type == disaster_type)
    if severity:
        stmt = stmt.where(DisasterEvent.severity == severity)
    if year:
        stmt = stmt.where(
            (DisasterEvent.event_date >= dt.date(year, 1, 1))
            & (DisasterEvent.event_date <= dt.date(year, 12, 31))
        )
    if search:
        like = f"%{search}%"
        stmt = stmt.where(DisasterEvent.name.ilike(like))
    stmt = stmt.order_by(DisasterEvent.event_date.desc().nullslast(), DisasterEvent.created_at.desc())
    events = list(db.execute(stmt).scalars().all())
    if province:
        events = [e for e in events if province in (e.province or [])]
    return events


def event_has_published_result(db: Session, event_id: int) -> bool:
    stmt = (
        select(AnalysisResult.id)
        .join(AnalysisRun, AnalysisRun.id == AnalysisResult.run_id)
        .where(AnalysisRun.event_id == event_id, AnalysisResult.is_published.is_(True))
        .limit(1)
    )
    return db.execute(stmt).first() is not None


# --- AOI -----------------------------------------------------------------


def create_aoi(db: Session, event_id: int, data: dict) -> DisasterAOI:
    aoi = DisasterAOI(
        event_id=event_id,
        geojson=data["geojson"],
        area_ha=data.get("area_ha"),
        centroid=data.get("centroid"),
        bbox=data.get("bbox"),
        source=data.get("source", "draw"),
    )
    db.add(aoi)
    db.commit()
    db.refresh(aoi)
    return aoi


def get_active_aoi(db: Session, event_id: int) -> DisasterAOI | None:
    stmt = (
        select(DisasterAOI)
        .where(DisasterAOI.event_id == event_id)
        .order_by(DisasterAOI.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def get_aoi(db: Session, aoi_id: int) -> DisasterAOI | None:
    return db.get(DisasterAOI, aoi_id)


# --- Imagery ---------------------------------------------------------------


def add_imagery(db: Session, event_id: int, data: dict) -> SatelliteImagery:
    img = SatelliteImagery(
        event_id=event_id,
        phase=data["phase"],
        satellite=data["satellite"],
        acquisition_date=data["acquisition_date"],
        sensor=data.get("sensor"),
        resolution_m=data.get("resolution_m"),
        cloud_coverage_pct=data.get("cloud_coverage_pct"),
        data_source=data.get("data_source"),
        is_primary=bool(data.get("is_primary", False)),
        preview_tile_url=data.get("preview_tile_url"),
    )
    if img.is_primary:
        _clear_primary(db, event_id, img.phase)
    db.add(img)
    db.commit()
    db.refresh(img)
    return img


def _clear_primary(db: Session, event_id: int, phase: str) -> None:
    stmt = select(SatelliteImagery).where(
        SatelliteImagery.event_id == event_id, SatelliteImagery.phase == phase, SatelliteImagery.is_primary.is_(True)
    )
    for row in db.execute(stmt).scalars().all():
        row.is_primary = False


def set_primary_imagery(db: Session, event_id: int, imagery_id: int) -> SatelliteImagery:
    img = db.get(SatelliteImagery, imagery_id)
    if img is None or img.event_id != event_id:
        raise ValueError("Imagery not found for this event")
    _clear_primary(db, event_id, img.phase)
    img.is_primary = True
    db.commit()
    db.refresh(img)
    return img


def list_imagery(db: Session, event_id: int, phase: str | None = None) -> list[SatelliteImagery]:
    stmt = select(SatelliteImagery).where(SatelliteImagery.event_id == event_id)
    if phase:
        stmt = stmt.where(SatelliteImagery.phase == phase)
    stmt = stmt.order_by(SatelliteImagery.acquisition_date.asc())
    return list(db.execute(stmt).scalars().all())


def get_primary_imagery(db: Session, event_id: int, phase: str) -> SatelliteImagery | None:
    rows = list_imagery(db, event_id, phase)
    primary = [r for r in rows if r.is_primary]
    if primary:
        return primary[0]
    return rows[-1] if rows else None


def get_imagery(db: Session, imagery_id: int) -> SatelliteImagery | None:
    return db.get(SatelliteImagery, imagery_id)


# --- Analysis runs / results -----------------------------------------------


def create_run(db: Session, event_id: int, data: dict, created_by: int | None) -> AnalysisRun:
    run = AnalysisRun(
        event_id=event_id,
        model_id=data["model_id"],
        model_version=data.get("model_version"),
        aoi_id=data["aoi_id"],
        pre_imagery_id=data.get("pre_imagery_id"),
        post_imagery_id=data.get("post_imagery_id"),
        status="queued",
        created_by=created_by,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_run(db: Session, run_id: int) -> AnalysisRun | None:
    return db.get(AnalysisRun, run_id)


def list_runs_for_event(db: Session, event_id: int) -> list[AnalysisRun]:
    stmt = select(AnalysisRun).where(AnalysisRun.event_id == event_id).order_by(AnalysisRun.created_at.desc())
    return list(db.execute(stmt).scalars().all())


def get_result_for_run(db: Session, run_id: int) -> AnalysisResult | None:
    stmt = select(AnalysisResult).where(AnalysisResult.run_id == run_id)
    return db.execute(stmt).scalars().first()


def upsert_result(db: Session, run_id: int, data: dict) -> AnalysisResult:
    """Replaces the previous result for a run on re-run (spec: "Re-run" keeps
    one current result per run, publish history tracked via
    `publication_version`, not one row per attempt)."""
    result = get_result_for_run(db, run_id)
    if result is None:
        result = AnalysisResult(run_id=run_id)
        db.add(result)
    result.tile_url = data.get("tile_url")
    result.statistics = data.get("statistics")
    result.features = data.get("features")
    result.legend = data.get("legend")
    result.confidence_summary = data.get("confidence_summary")
    db.commit()
    db.refresh(result)
    return result


def publish_result(db: Session, result: AnalysisResult, published_by: int | None) -> AnalysisResult:
    result.is_published = True
    result.published_at = dt.datetime.now(dt.timezone.utc)
    result.published_by = published_by
    result.publication_version = (result.publication_version or 0) + 1
    db.commit()
    db.refresh(result)
    return result


def unpublish_result(db: Session, result: AnalysisResult) -> AnalysisResult:
    result.is_published = False
    db.commit()
    db.refresh(result)
    return result


def list_published_analyses(db: Session, event_id: int) -> list[tuple[AnalysisRun, AnalysisResult]]:
    stmt = (
        select(AnalysisRun, AnalysisResult)
        .join(AnalysisResult, AnalysisResult.run_id == AnalysisRun.id)
        .where(AnalysisRun.event_id == event_id, AnalysisResult.is_published.is_(True))
    )
    return [(row[0], row[1]) for row in db.execute(stmt).all()]


def get_published_analysis(db: Session, event_id: int, model_id: str) -> tuple[AnalysisRun, AnalysisResult] | None:
    for run, result in list_published_analyses(db, event_id):
        if run.model_id == model_id:
            return run, result
    return None


# --- Hotspots ----------------------------------------------------------------


def create_hotspot(db: Session, event_id: int, data: dict, created_by: int | None) -> Hotspot:
    hotspot = Hotspot(
        event_id=event_id,
        analysis_result_id=data.get("analysis_result_id"),
        name=data["name"],
        impact_level=data["impact_level"],
        geojson=data["geojson"],
        stats=data.get("stats"),
        is_published=bool(data.get("is_published", False)),
        created_by=created_by,
    )
    db.add(hotspot)
    db.commit()
    db.refresh(hotspot)
    return hotspot


def list_hotspots(db: Session, event_id: int, published_only: bool) -> list[Hotspot]:
    stmt = select(Hotspot).where(Hotspot.event_id == event_id)
    if published_only:
        stmt = stmt.where(Hotspot.is_published.is_(True))
    return list(db.execute(stmt).scalars().all())
