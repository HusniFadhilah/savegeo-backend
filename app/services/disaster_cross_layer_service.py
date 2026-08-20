"""Cross-layer impact statistics (spec section 28) - computed on the backend
from GEE mask intersection, never in the frontend/LLM. Only Flood x Forest is
real: it's the only pair where both sides have an actually-running model
(`flood_change_v1` + `forest_change_v1`). Flood x Building / Flood x Road are
not computable - Building/Road models are `enabled: False` (see
`disaster_model_registry.py`) - so this module simply returns `None` for
those, and callers must render "Not Available", never a fabricated number.

Geometry stays in GEE the whole time (no PostGIS/shapely in this repo -
matches the existing convention: GEE does the spatial math, Postgres only
stores JSONB). Both masks are re-derived at request time from the same
pre/post imagery dates the two published runs used - cheap relative to a
full analysis run since it's just two band-math passes + one reduceRegion.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.repositories import disaster_repo
from app.services import disaster_analysis_service as das
from app.services.gee_common import AnalysisError, _event_area_ha, create_geometry_from_payload


def compute_flood_forest_overlay(db: Session, event_id: int) -> dict | None:
    flood = disaster_repo.get_published_analysis(db, event_id, "flood_change_v1")
    forest = disaster_repo.get_published_analysis(db, event_id, "forest_change_v1")
    if flood is None or forest is None:
        return None
    flood_run, _ = flood
    forest_run, _ = forest

    flood_aoi_row = disaster_repo.get_aoi(db, flood_run.aoi_id)
    if flood_aoi_row is None:
        return None
    aoi = create_geometry_from_payload({"geojson": flood_aoi_row.geojson})

    flood_pre_img = disaster_repo.get_imagery(db, flood_run.pre_imagery_id) if flood_run.pre_imagery_id else None
    flood_post_img = disaster_repo.get_imagery(db, flood_run.post_imagery_id) if flood_run.post_imagery_id else None
    forest_post_img = disaster_repo.get_imagery(db, forest_run.post_imagery_id) if forest_run.post_imagery_id else None
    if not (flood_pre_img and flood_post_img and forest_post_img):
        return None

    try:
        _pre_water, post_water = das.flood_water_masks(aoi, flood_pre_img.acquisition_date, flood_post_img.acquisition_date)
        forest_now = das.forest_mask(aoi, forest_post_img.acquisition_date)
    except AnalysisError:
        return None
    if forest_now is None:
        return None

    overlap = post_water.And(forest_now)
    overlap_ha = _event_area_ha(overlap.selfMask(), aoi, 30)

    return {
        "layers": ["flood_change_v1", "forest_change_v1"],
        "label": "Forest inside flood extent",
        "forest_in_flood_extent_ha": overlap_ha,
    }


def get_available_cross_layer_stats(db: Session, event_id: int) -> list[dict]:
    """Returns every cross-layer stat that could actually be computed for
    this event (currently only flood x forest). Building/Road are omitted
    entirely, not returned as null placeholders - the User dashboard renders
    "Not Available" only for combinations it explicitly knows about, and
    simply doesn't offer combinations that were never registered."""
    stats = []
    flood_forest = compute_flood_forest_overlay(db, event_id)
    if flood_forest is not None:
        stats.append(flood_forest)
    return stats
