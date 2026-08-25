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
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
from app.db.models.disaster_event import DisasterEvent
from app.db.session import get_db
from app.registries import disaster_model_registry
from app.repositories import disaster_repo
from app.services.disaster_cross_layer_service import get_available_cross_layer_stats

router = APIRouter(prefix="/disasters", tags=["disasters"])


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
    for model in disaster_model_registry.list_models(enabled_only=True):
        model_id = model["model_id"]
        published = disaster_repo.get_published_analysis(db, event_id, model_id)
        if published is not None:
            run, result = published
            entries.append({
                "model_id": model_id,
                "user_label": model["user_label"],
                "category": model["category"],
                "available": True,
                "run": run.to_dict(),
                "result": result.to_dict(),
            })
        else:
            entries.append({
                "model_id": model_id,
                "user_label": model["user_label"],
                "category": model["category"],
                "available": False,
                "run": None,
                "result": None,
            })
    return entries


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
        data = event.to_dict()
        data["available_analysis_count"] = len(disaster_repo.list_published_analyses(db, event.id))
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
            "pre": [img.to_dict() for img in pre_imagery],
            "post": [img.to_dict() for img in post_imagery],
        },
        "primary_imagery": {
            "pre": primary_pre.to_dict() if primary_pre else None,
            "post": primary_post.to_dict() if primary_post else None,
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
            "pre_tile_url": primary_pre.preview_tile_url if primary_pre else None,
            "post_tile_url": primary_post.preview_tile_url if primary_post else None,
        },
        "analyses": _build_analyses(db, event_id),
    }


@router.get("/{event_id}/statistics")
def get_disaster_statistics(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    kpis = {}
    for run, result in disaster_repo.list_published_analyses(db, event_id):
        kpis[run.model_id] = result.statistics
    return {
        "kpis": kpis,
        "cross_layer": get_available_cross_layer_stats(db, event_id),
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
