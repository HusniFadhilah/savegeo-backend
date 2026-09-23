"""Public user-facing Disaster Intelligence Dashboard routes - prefix
`/disasters` (NOT `/disaster`, the legacy BMKG/DEM/sources router in
`disaster.py`). Every route requires a disaster viewer token (public user or
admin).

Publish boundary: every event/analysis/hotspot lookup here is published-only.
An existing-but-unpublished/draft event 404s - it must never leak that a draft
exists, so lookups never distinguish "not found" from "found but not
published" in the response. `_get_published_event` is the single choke point
every route uses to enforce this.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
from app.db.models.disaster_event import DisasterEvent
from app.db.models.hotspot import Hotspot
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.db.session import get_db
from app.registries import disaster_model_registry
from app.repositories import disaster_repo
from app.services.disaster_cross_layer_service import get_available_cross_layer_stats
from app.services import firms_service, wildfire_hotspot_service

router = APIRouter(prefix="/disasters", tags=["disasters"])


def _viewer_imagery_dict(img) -> dict:
    data = img.to_dict()
    # Rows created before the public local-raster route existed still contain
    # the admin-only preview URL. Normalize them at the published boundary so
    # existing BlackSky/GeoTIFF events start working without a data migration.
    old_prefix = "/api/admin/disasters/imagery-tiles/"
    if data.get("source_kind") == "local_upload" and str(data.get("preview_tile_url") or "").startswith(old_prefix):
        data["preview_tile_url"] = data["preview_tile_url"].replace(old_prefix, "/api/disasters/imagery-tiles/", 1)
    return data


def _get_published_event(db: Session, event_id: int) -> DisasterEvent:
    event = disaster_repo.get_event(db, event_id)
    if event is None or event.status != "published":
        raise HTTPException(status_code=404, detail="Disaster event not found")
    return event


def _build_analyses(db: Session, event_id: int) -> list[dict]:
    """One entry per enabled registry model - `available=True` only if a
    published result exists for that model on this event. Disabled models are
    omitted entirely (hidden); enabled-but-not-yet-run models are listed with
    `available: False` so User sees "Not Available" instead of the item
    vanishing (contract doc, section B)."""
    entries = []
    event = disaster_repo.get_event(db, event_id)
    for model in disaster_model_registry.list_models(enabled_only=True, disaster_type=event.disaster_type if event else None):
        model_id = model["model_id"]
        published = disaster_repo.get_published_analysis(db, event_id, model_id)
        if published is not None and published[0].status in ("completed", "review_required", "published"):
            run, result = published
            entries.append({
                "model_id": model_id,
                "user_label": model["user_label"],
                "category": model["category"],
                "result_semantics": model.get("result_semantics"),
                "damage_model": bool(model.get("damage_model", False)),
                "validation_status": model.get("validation_status"),
                "limitations": list(model.get("limitations", [])),
                "available": True,
                "run": run.to_dict(),
                "result": result.to_dict(),
            })
        else:
            entries.append({
                "model_id": model_id,
                "user_label": model["user_label"],
                "category": model["category"],
                "result_semantics": model.get("result_semantics"),
                "damage_model": bool(model.get("damage_model", False)),
                "validation_status": model.get("validation_status"),
                "limitations": list(model.get("limitations", [])),
                "available": False,
                "run": None,
                "result": None,
            })
    return entries


def _compatible_model_ids(event: DisasterEvent) -> set[str]:
    """Return only models explicitly registered for this event type.

    Persisted runs can outlive a registry change. Filtering at the published
    API boundary prevents a flood page from displaying stale forest/generic
    KPIs that happen to share the same event row.
    """
    return {model["model_id"] for model in disaster_model_registry.list_models(
        enabled_only=True, disaster_type=event.disaster_type,
    )}


def _wildfire_metrics(db: Session, event: DisasterEvent) -> tuple[int, int]:
    """Return persisted hotspot totals for a forest-fire event.

    The generic disaster list is also used by the Karhutla overview card. Keep
    that card backed by the same persisted NASA FIRMS table as the standalone
    wildfire API instead of letting the frontend interpret missing fields as 0.
    """
    rows = list(
        db.execute(
            select(WildfireHotspot.stats).where(
                WildfireHotspot.event_id == event.id,
                WildfireHotspot.is_published.is_(True),
            )
        ).scalars()
    )
    if rows:
        high_confidence = sum(
            str((stats or {}).get("confidence_category", (stats or {}).get("confidence_label", ""))).casefold()
            == "high"
            for stats in rows
        )
        return len(rows), high_confidence

    # Preserve compatibility with older admin-curated hotspot rows when the
    # persisted NASA table is still empty.
    curated_count = db.execute(
        select(Hotspot.id).where(
            Hotspot.event_id == event.id,
            Hotspot.is_published.is_(True),
        )
    ).scalars().all()
    return len(curated_count), 0


@router.get("")
def list_disasters(
    disaster_type: str | None = None,
    year: int | None = None,
    province: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    events = disaster_repo.list_events(
        db,
        published_only=True,
        disaster_type=disaster_type,
        province=province,
        severity=severity,
        year=year,
        search=search,
    )
    result = []
    for event in events:
        if event.disaster_type == "forest_fire":
            # Populate the cache only once. Subsequent overview/detail reads
            # are served from the database until an explicit admin sync.
            try:
                wildfire_hotspot_service.ensure_initial_sync(db, event)
            except firms_service.FirmsRequestError:
                # A source outage must not hide the published event. Existing
                # database rows, if any, remain usable below.
                pass
        data = event.to_dict()
        allowed = _compatible_model_ids(event)
        data["available_analysis_count"] = sum(
            1 for run, _ in disaster_repo.list_published_analyses(db, event.id)
            if run.model_id in allowed
        )
        if event.disaster_type == "forest_fire":
            hotspot_count, high_confidence_count = _wildfire_metrics(db, event)
            data.update(
                {
                    "hotspot_count": hotspot_count,
                    "high_confidence_count": high_confidence_count,
                    "burned_area_ha": None,
                    "burned_area_source": None,
                }
            )
        result.append(data)
    return {"events": result}


@router.get("/{event_id}")
def get_disaster(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    event = _get_published_event(db, event_id)
    aoi = disaster_repo.get_active_aoi(db, event_id)
    pre_imagery = disaster_repo.list_imagery(db, event_id, phase="pre")
    post_imagery = disaster_repo.list_imagery(db, event_id, phase="post")
    primary_pre = disaster_repo.get_primary_imagery(db, event_id, "pre")
    primary_post = disaster_repo.get_primary_imagery(db, event_id, "post")
    return {
        "event": event.to_dict(),
        "aoi": aoi.to_dict() if aoi else None,
        "imagery": {
            "pre": [_viewer_imagery_dict(img) for img in pre_imagery],
            "post": [_viewer_imagery_dict(img) for img in post_imagery],
        },
        "primary_imagery": {
            "pre": _viewer_imagery_dict(primary_pre) if primary_pre else None,
            "post": _viewer_imagery_dict(primary_post) if primary_post else None,
        },
    }


@router.get("/{event_id}/analyses")
def get_disaster_analyses(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    return {"analyses": _build_analyses(db, event_id)}


@router.get("/{event_id}/layers")
def get_disaster_layers(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    primary_pre = disaster_repo.get_primary_imagery(db, event_id, "pre")
    primary_post = disaster_repo.get_primary_imagery(db, event_id, "post")
    return {
        "satellite": {
            "pre_tile_url": _viewer_imagery_dict(primary_pre).get("preview_tile_url") if primary_pre else None,
            "post_tile_url": _viewer_imagery_dict(primary_post).get("preview_tile_url") if primary_post else None,
        },
        "analyses": _build_analyses(db, event_id),
    }


@router.get("/{event_id}/statistics")
def get_disaster_statistics(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    event = _get_published_event(db, event_id)
    allowed = _compatible_model_ids(event)
    kpis = {}
    for run, result in disaster_repo.list_published_analyses(db, event_id):
        if run.model_id not in allowed or not result.statistics:
            continue
        kpis[run.model_id] = result.to_dict()["statistics"]
    cross_layer = [
        stat for stat in get_available_cross_layer_stats(db, event_id)
        if all(layer in allowed for layer in stat.get("layers", []))
    ]
    return {
        "event": {
            "id": event.id,
            "name": event.name,
            "disaster_type": event.disaster_type,
            "event_date": event.event_date.isoformat() if event.event_date else None,
            "start_date": event.start_date.isoformat() if event.start_date else None,
            "end_date": event.end_date.isoformat() if event.end_date else None,
        },
        "kpis": kpis,
        "cross_layer": cross_layer,
    }


@router.get("/{event_id}/hotspots")
def get_disaster_hotspots(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    hotspots = disaster_repo.list_hotspots(db, event_id, published_only=True)
    return {"hotspots": [h.to_dict() for h in hotspots]}


@router.get("/{event_id}/features")
def get_disaster_features(
    event_id: int,
    analysis: str | None = None,
    bbox: str | None = None,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    """Always an empty FeatureCollection for MVP - none of the 3 real models
    produce per-object `features` (see contract doc intro). Kept as a real,
    published-gated endpoint so the frontend can call it without special-
    casing, and so the route shape is future-proof once a feature-producing
    model exists."""
    _get_published_event(db, event_id)
    return {
        "features": {"type": "FeatureCollection", "features": []},
        "note": "Per-feature detail is not available for this event's analyses yet.",
    }
