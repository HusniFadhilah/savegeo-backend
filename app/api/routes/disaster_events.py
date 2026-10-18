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

import math
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from shapely.geometry import box, mapping, shape
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
from app.core.viewer_features import require_feature
from app.db.models.disaster_event import DisasterEvent
from app.db.models.disaster_aoi import DisasterAOI
from app.db.models.analysis_result import AnalysisResult
from app.db.models.analysis_run import AnalysisRun
from app.services.disaster_completeness_service import build_completeness
from app.db.models.hotspot import Hotspot
from app.db.models.satellite_imagery import SatelliteImagery
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.db.session import get_db
from app.registries import disaster_model_registry
from app.repositories import disaster_repo
from app.services.disaster_cross_layer_service import get_available_cross_layer_stats
from app.services import disaster_capability_service
from app.services import samgeo_service
from app.services import local_imagery_tile_service
from app.services.gee_common import AnalysisError, public_analysis_error
from app.services.disaster_analysis_service import serialize_result_for_view

router = APIRouter(
    prefix="/disasters",
    tags=["disasters"],
    dependencies=[Depends(require_feature("app.feature.disaster_mapping"))],
)


class EventSegmentationRequest(BaseModel):
    imagery_id: int
    mode: Literal["automatic", "prompt_based"] = "prompt_based"
    class_label: Literal["building", "road", "water", "tree", "crop", "bare_land", "custom_prompt"] = "building"
    prompt: str | None = Field(default=None, max_length=120)
    confidence_threshold: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    center_crop_m: Literal[500, 1000, 2000] = 1000


@router.get("/segmentation/status")
def event_segmentation_status(viewer=Depends(get_current_disaster_viewer)):
    return samgeo_service.capabilities()


def _segmentation_owner(event_id, viewer):
    return f"disaster:{event_id}:{type(viewer).__name__}:{viewer.id}"


@router.post("/{event_id}/segmentation/jobs", status_code=202)
def start_event_segmentation(event_id: int, payload: EventSegmentationRequest,
                             viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    image = disaster_repo.get_imagery(db, payload.imagery_id)
    if image is None or image.event_id != event_id:
        raise HTTPException(404, "Imagery event tidak ditemukan")
    if image.source_kind != "local_upload" or not image.local_file_path:
        raise HTTPException(422, "Segmentasi objek memerlukan GeoTIFF resolusi tinggi milik event; preview tile Sentinel bukan raster RGB")
    if image.resolution_m is None or image.resolution_m > 3:
        raise HTTPException(422, "Segmentasi bangunan/jalan memerlukan metadata resolusi citra maksimum 3 m")
    aoi_row = disaster_repo.get_active_aoi(db, event_id)
    if aoi_row is None:
        raise HTTPException(422, "AOI event belum tersedia")
    geojson = aoi_row.geojson
    aoi = shape(geojson.get("geometry", geojson))
    center = aoi.representative_point()
    half = payload.center_crop_m/2
    dx, dy = half/(111320*math.cos(math.radians(center.y))), half/111320
    crop = aoi.intersection(box(center.x-dx, center.y-dy, center.x+dx, center.y+dy))
    prompt = payload.prompt if payload.class_label == "custom_prompt" else payload.class_label.replace("_", " ")
    try:
        return samgeo_service.start_job({
            "local_file_path": image.local_file_path, "_uploaded_local_file": True,
            "aoi": {"type": "Feature", "geometry": mapping(crop), "properties": {}},
            "mode": payload.mode, "prompt": prompt if payload.mode == "prompt_based" else None,
            "class_label": payload.class_label if payload.mode == "prompt_based" else None,
            "confidence_threshold": payload.confidence_threshold,
            "scene_id": image.scene_id or f"event-imagery-{image.id}", "imagery_id": image.id,
            "acquisition_date": image.acquisition_date.isoformat(), "source_resolution_m": image.resolution_m,
        }, owner=_segmentation_owner(event_id, viewer))
    except AnalysisError as exc:
        raise HTTPException(exc.status_code, public_analysis_error(exc)) from exc


@router.get("/{event_id}/segmentation/jobs/{job_id}")
def get_event_segmentation(event_id: int, job_id: str, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    try:
        return samgeo_service.get_job(job_id, owner=_segmentation_owner(event_id, viewer))
    except AnalysisError as exc:
        raise HTTPException(exc.status_code, public_analysis_error(exc)) from exc


def _viewer_imagery_dict(img) -> dict:
    data = img.to_dict()
    if data.get("source_kind") == "local_upload" and data.get("local_file_path"):
        # GeoTIFF pixel spacing is more reliable than manually entered upload
        # metadata. This also corrects existing imagery rows without a DB
        # migration; the result is cached per raster path by the tile service.
        raster_resolution_m = local_imagery_tile_service.local_raster_resolution_m(
            data["local_file_path"]
        )
        if raster_resolution_m is not None:
            data["resolution_m"] = raster_resolution_m

    # Rows created before the public local-raster route existed still contain
    # the admin-only preview URL. Normalize them at the published boundary so
    # existing BlackSky/GeoTIFF events start working without a data migration.
    old_prefix = "/api/admin/disasters/imagery-tiles/"
    if data.get("source_kind") == "local_upload" and str(data.get("preview_tile_url") or "").startswith(old_prefix):
        data["preview_tile_url"] = data["preview_tile_url"].replace(old_prefix, "/api/disasters/imagery-tiles/", 1)
    return data


def _get_published_event(db: Session, event_id: int) -> DisasterEvent:
    event = disaster_repo.get_event(db, event_id)
    if event is None or event.status != "published" or not event.is_public:
        raise HTTPException(status_code=404, detail="Disaster event not found")
    return event


def _build_analyses(db: Session, event_id: int, *, force_refresh_tiles: bool = False) -> list[dict]:
    """One entry per enabled registry model - `available=True` only if a
    published result exists for that model on this event. Disabled models are
    omitted entirely (hidden); enabled-but-not-yet-run models are listed with
    `available: False` so User sees "Not Available" instead of the item
    vanishing (contract doc, section B)."""
    entries = []
    event = disaster_repo.get_event(db, event_id)
    models = disaster_model_registry.list_models(enabled_only=True, disaster_type=event.disaster_type if event else None)
    if event and event.disaster_type == "other":
        models.extend(
            model for model in disaster_model_registry.list_models(enabled_only=False, disaster_type="other")
            if model.get("result_semantics") == "not_available"
        )
    for model in models:
        model_id = model["model_id"]
        published = disaster_repo.get_published_analysis(db, event_id, model_id)
        if published is not None and published[0].status in ("completed", "review_required", "published"):
            run, result = published
            capability = disaster_capability_service.validate_persisted_result(db, event, run, result)
        else:
            capability = None
        if published is not None and capability and capability.allowed:
            entries.append({
                "model_id": model_id,
                "user_label": model["user_label"],
                "category": model["category"],
                "result_semantics": model.get("result_semantics"),
                "damage_model": bool(model.get("damage_model", False)),
                "validation_status": model.get("validation_status"),
                "limitations": list(model.get("limitations", [])),
                "capability_status": capability.status,
                "available": True,
                "run": run.to_dict(),
                "result": serialize_result_for_view(db, run, result, include_features=(
                    model.get("output_type") in {"feature_collection", "polygon"}
                    or str(model.get("output_type", "")).startswith("polygon")
                ), force_refresh_tiles=force_refresh_tiles),
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
                "capability_status": capability.status if capability else disaster_capability_service.model_state(model),
                "availability_reason": list(capability.reasons) if capability else ["Belum ada hasil AnalysisResult yang dipublikasikan"],
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


def _wildfire_metrics_by_event(db: Session, events: list[DisasterEvent]) -> dict[int, tuple[int, int]]:
    """Aggregate FIRMS card metrics in SQL without loading every hotspot JSON row."""
    event_ids = [event.id for event in events if event.disaster_type == "forest_fire"]
    if not event_ids:
        return {}

    confidence = func.lower(func.coalesce(
        WildfireHotspot.stats["confidence_category"].as_string(),
        WildfireHotspot.stats["confidence_label"].as_string(),
        "",
    ))
    wildfire_rows = db.execute(
        select(
            WildfireHotspot.event_id,
            func.count(WildfireHotspot.id),
            func.coalesce(func.sum(case((confidence == "high", 1), else_=0)), 0),
        )
        .where(
            WildfireHotspot.event_id.in_(event_ids),
            WildfireHotspot.is_published.is_(True),
        )
        .group_by(WildfireHotspot.event_id)
    ).all()
    metrics = {event_id: (int(total), int(high or 0)) for event_id, total, high in wildfire_rows}

    # Preserve compatibility with older admin-curated hotspot rows when FIRMS
    # has not yet stored any published observations for an event.
    missing_ids = [event_id for event_id in event_ids if event_id not in metrics]
    if missing_ids:
        curated_rows = db.execute(
            select(Hotspot.event_id, func.count(Hotspot.id))
            .where(Hotspot.event_id.in_(missing_ids), Hotspot.is_published.is_(True))
            .group_by(Hotspot.event_id)
        ).all()
        metrics.update({event_id: (int(total), 0) for event_id, total in curated_rows})

    return {event_id: metrics.get(event_id, (0, 0)) for event_id in event_ids}


def _available_analysis_counts(db: Session, events: list[DisasterEvent]) -> dict[int, int]:
    """Validate published analyses with a few batched reads instead of N+1 queries."""
    if not events:
        return {}

    events_by_id = {event.id: event for event in events}
    allowed_by_event = {event.id: _compatible_model_ids(event) for event in events}
    model_ids = set().union(*allowed_by_event.values())
    if not model_ids:
        return {event.id: 0 for event in events}

    rows = db.execute(
        select(
            AnalysisRun.id,
            AnalysisRun.event_id,
            AnalysisRun.model_id,
            AnalysisRun.status,
            AnalysisRun.aoi_id,
            AnalysisRun.pre_imagery_id,
            AnalysisRun.post_imagery_id,
            AnalysisRun.parameters,
            AnalysisResult.is_published,
            AnalysisResult.tile_url,
            AnalysisResult.statistics,
            AnalysisResult.provenance,
            AnalysisResult.coverage,
            AnalysisResult.features.is_not(None).label("has_features"),
        )
        .join(AnalysisResult, AnalysisResult.run_id == AnalysisRun.id)
        .where(
            AnalysisRun.event_id.in_(events_by_id),
            AnalysisRun.model_id.in_(model_ids),
            AnalysisRun.status.in_(["completed", "review_required", "published"]),
            AnalysisResult.is_published.is_(True),
        )
    ).all()
    candidates = [
        row for row in rows
        if row.model_id in allowed_by_event.get(row.event_id, set())
    ]
    if not candidates:
        return {event.id: 0 for event in events}

    aoi_ids = {row.aoi_id for row in candidates}
    imagery_ids = {
        imagery_id
        for row in candidates
        for imagery_id in (row.pre_imagery_id, row.post_imagery_id)
        if imagery_id is not None
    }
    aois = {
        aoi.id: aoi
        for aoi in db.execute(select(DisasterAOI).where(DisasterAOI.id.in_(aoi_ids))).scalars()
    }
    imagery = {
        image.id: image
        for image in db.execute(select(SatelliteImagery).where(SatelliteImagery.id.in_(imagery_ids))).scalars()
    } if imagery_ids else {}

    available_by_event: dict[int, set[str]] = {event.id: set() for event in events}
    for row in candidates:
        run = SimpleNamespace(
            id=row.id,
            event_id=row.event_id,
            model_id=row.model_id,
            status=row.status,
            aoi_id=row.aoi_id,
            pre_imagery_id=row.pre_imagery_id,
            post_imagery_id=row.post_imagery_id,
            parameters=row.parameters,
        )
        result = SimpleNamespace(
            is_published=row.is_published,
            tile_url=row.tile_url,
            statistics=row.statistics,
            provenance=row.provenance,
            coverage=row.coverage,
        )
        capability = disaster_capability_service.validate_persisted_result(
            db,
            events_by_id[row.event_id],
            run,
            result,
            current_inputs=(
                aois.get(row.aoi_id),
                imagery.get(row.pre_imagery_id),
                imagery.get(row.post_imagery_id),
            ),
            has_features_override=bool(row.has_features),
        )
        if capability.allowed:
            available_by_event[row.event_id].add(row.model_id)

    return {event_id: len(model_ids) for event_id, model_ids in available_by_event.items()}


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
    analysis_counts = _available_analysis_counts(db, events)
    wildfire_metrics = _wildfire_metrics_by_event(db, events)
    result = []
    for event in events:
        # Listing published events must stay a database-only read. FIRMS sync
        # can require several upstream requests and is performed explicitly by
        # the admin sync workflow, never while a user opens this page.
        data = event.to_dict()
        data["available_analysis_count"] = analysis_counts.get(event.id, 0)
        if event.disaster_type == "forest_fire":
            hotspot_count, high_confidence_count = wildfire_metrics.get(event.id, (0, 0))
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
def get_disaster_layers(event_id: int, refresh_tiles: bool = False, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    _get_published_event(db, event_id)
    primary_pre = disaster_repo.get_primary_imagery(db, event_id, "pre")
    primary_post = disaster_repo.get_primary_imagery(db, event_id, "post")
    return {
        "satellite": {
            "pre_tile_url": _viewer_imagery_dict(primary_pre).get("preview_tile_url") if primary_pre else None,
            "post_tile_url": _viewer_imagery_dict(primary_post).get("preview_tile_url") if primary_post else None,
        },
        "analyses": _build_analyses(db, event_id, force_refresh_tiles=refresh_tiles),
    }


@router.get("/{event_id}/statistics")
def get_disaster_statistics(event_id: int, viewer=Depends(get_current_disaster_viewer), db: Session = Depends(get_db)):
    event = _get_published_event(db, event_id)
    allowed = _compatible_model_ids(event)
    kpis = {}
    for run, result in disaster_repo.list_published_analyses(db, event_id):
        if run.model_id not in allowed or not result.statistics:
            continue
        capability = disaster_capability_service.validate_persisted_result(db, event, run, result)
        if not capability.allowed:
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
        "completeness": build_completeness(event.disaster_type, _build_analyses(db, event_id)),
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
    """Return feature outputs only from currently compatible published results."""
    event = _get_published_event(db, event_id)
    features = []
    for run, result in disaster_repo.list_published_analyses(db, event_id):
        if analysis and run.model_id != analysis:
            continue
        if not disaster_capability_service.validate_persisted_result(db, event, run, result).allowed:
            continue
        collection = result.features or {}
        if collection.get("type") == "FeatureCollection":
            features.extend(collection.get("features", []))
    return {
        "features": {"type": "FeatureCollection", "features": features},
        "note": None if features else "Per-feature detail is not available for compatible published analyses.",
    }
