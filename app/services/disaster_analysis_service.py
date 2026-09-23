"""Validate and execute configured disaster analyses; persist status and reusable results.

Hosted Dynamic World predictions are compared independently on one common grid.
Existing SAR/index methods remain explicitly labelled alternatives.
"""
from __future__ import annotations

import datetime as dt
import logging

import ee
from sqlalchemy.orm import Session

from app.registries.disaster_model_registry import get_model
from app.repositories import disaster_repo
from app.services.disaster_capability_service import (
    build_provenance,
    check_inputs,
    model_state,
)
from app.services.gee_common import (
    AnalysisError,
    _event_area_ha,
    create_geometry_from_payload,
    get_tile_url,
)

logger = logging.getLogger(__name__)

_IMAGERY_WINDOW_DAYS = 5  # +/- window around an admin-picked acquisition_date to build a cloud-safe composite


def _window(center: dt.date, days: int = _IMAGERY_WINDOW_DAYS) -> tuple[str, str]:
    start = center - dt.timedelta(days=days)
    end = center + dt.timedelta(days=days) + dt.timedelta(days=1)  # filterDate end is exclusive
    return start.isoformat(), end.isoformat()


def _mask_s2_sr_clouds(image):
    scl = image.select("SCL")
    mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))
    return image.updateMask(mask).divide(10000)


# --- flood_change_v1 --------------------------------------------------------


def _s1_composite(aoi, center: dt.date):
    start, end = _window(center)
    col = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .select("VV")
    )
    if col.size().getInfo() == 0:
        return None
    return col.median().focal_median(30, "circle", "meters").clip(aoi)


def flood_water_masks(aoi, pre_date: dt.date, post_date: dt.date) -> tuple:
    """Returns (pre_water_mask, post_water_mask) booleans. Shared with
    `disaster_cross_layer_service` so cross-layer overlays reuse the exact
    same detection logic instead of re-deriving it differently."""
    before = _s1_composite(aoi, pre_date)
    after = _s1_composite(aoi, post_date)
    if before is None or after is None:
        raise AnalysisError("Data Sentinel-1 SAR tidak cukup untuk tanggal pre/post yang dipilih", 404)
    water_threshold = -16.0
    return before.lt(water_threshold), after.lt(water_threshold)


def _compute_flood_change(aoi, pre_date: dt.date, post_date: dt.date, scale: int = 30) -> dict:
    pre_water, post_water = flood_water_masks(aoi, pre_date, post_date)

    new_inundation = post_water.And(pre_water.Not())
    existing_water = post_water.And(pre_water)
    receded = pre_water.And(post_water.Not())

    new_ha = _event_area_ha(new_inundation.selfMask(), aoi, scale)
    existing_ha = _event_area_ha(existing_water.selfMask(), aoi, scale)
    receded_ha = _event_area_ha(receded.selfMask(), aoi, scale)

    class_img = (
        ee.Image(0)
        .where(existing_water, 1)
        .where(new_inundation, 2)
        .where(receded, 3)
        .selfMask()
        .rename("flood_class")
    )
    tile = get_tile_url(class_img, {"min": 1, "max": 3, "palette": ["#1565c0", "#e53935", "#90a4ae"]}, "Flood Change")

    return {
        "tile_url": tile["tile_url"] if tile else None,
        "statistics": {
            "flooded_area_ha": round(new_ha + existing_ha, 2),
            "new_inundation_ha": new_ha,
            "existing_water_ha": existing_ha,
            "receded_water_ha": receded_ha,
        },
        "legend": [
            {"label": "Existing Water", "color": "#1565c0"},
            {"label": "New Inundation", "color": "#e53935"},
            {"label": "Receded Water", "color": "#90a4ae"},
        ],
        "confidence_summary": None,
        "features": None,
    }


# --- water_segmentation_v1 ---------------------------------------------------


def _s2_ndwi(aoi, center: dt.date):
    start, end = _window(center)
    col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .map(_mask_s2_sr_clouds)
    )
    if col.size().getInfo() == 0:
        return None
    composite = col.median().clip(aoi)
    return composite.normalizedDifference(["B3", "B8"]).rename("NDWI")


def _compute_water_segmentation(aoi, pre_date: dt.date | None, post_date: dt.date | None, scale: int = 20) -> dict:
    if post_date is None:
        raise AnalysisError("Post-disaster imagery date wajib dipilih untuk Water Extent", 400)
    ndwi_post = _s2_ndwi(aoi, post_date)
    if ndwi_post is None:
        raise AnalysisError("Data Sentinel-2 bebas awan tidak cukup untuk tanggal yang dipilih", 404)

    threshold = 0.0
    water_post = ndwi_post.gt(threshold).selfMask()
    post_ha = _event_area_ha(water_post, aoi, scale)
    tile = get_tile_url(water_post, {"palette": ["#1565c0"], "min": 1, "max": 1}, "Water Extent")

    stats: dict = {"total_water_area_ha": post_ha}
    if pre_date is not None:
        ndwi_pre = _s2_ndwi(aoi, pre_date)
        if ndwi_pre is not None:
            water_pre = ndwi_pre.gt(threshold).selfMask()
            pre_ha = _event_area_ha(water_pre, aoi, scale)
            stats = {
                "pre_water_area_ha": pre_ha,
                "post_water_area_ha": post_ha,
                "change_ha": round(post_ha - pre_ha, 2),
            }

    return {
        "tile_url": tile["tile_url"] if tile else None,
        "statistics": stats,
        "legend": [{"label": "Water", "color": "#1565c0"}],
        "confidence_summary": None,
        "features": None,
    }


# --- forest_change_v1 (also serves "Forest Segmentation" single-date view) --


def _s2_ndvi(aoi, center: dt.date):
    start, end = _window(center)
    col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .map(_mask_s2_sr_clouds)
    )
    if col.size().getInfo() == 0:
        return None
    composite = col.median().clip(aoi)
    return composite.normalizedDifference(["B8", "B4"]).rename("NDVI")


_FOREST_NDVI_THRESHOLD = 0.4


def forest_mask(aoi, center: dt.date):
    """Returns a boolean forest mask for one date, or None if imagery is
    unavailable. Shared with `disaster_cross_layer_service`."""
    ndvi = _s2_ndvi(aoi, center)
    if ndvi is None:
        return None
    return ndvi.gt(_FOREST_NDVI_THRESHOLD)


def _compute_forest_change(aoi, pre_date: dt.date | None, post_date: dt.date, scale: int = 20) -> dict:
    forest_post = forest_mask(aoi, post_date)
    if forest_post is None:
        raise AnalysisError("Data Sentinel-2 bebas awan tidak cukup untuk tanggal post-disaster", 404)
    post_ha = _event_area_ha(forest_post.selfMask(), aoi, scale)

    if pre_date is None:
        tile = get_tile_url(forest_post.selfMask(), {"palette": ["#2e7d32"], "min": 1, "max": 1}, "Forest Cover")
        return {
            "tile_url": tile["tile_url"] if tile else None,
            "statistics": {"forest_area_ha": post_ha},
            "legend": [{"label": "Forest", "color": "#2e7d32"}],
            "confidence_summary": None,
            "features": None,
        }

    forest_pre = forest_mask(aoi, pre_date)
    if forest_pre is None:
        raise AnalysisError("Data Sentinel-2 bebas awan tidak cukup untuk tanggal pre-disaster", 404)
    pre_ha = _event_area_ha(forest_pre.selfMask(), aoi, scale)

    loss = forest_pre.And(forest_post.Not())
    gain = forest_post.And(forest_pre.Not())
    maintained = forest_pre.And(forest_post)

    loss_ha = _event_area_ha(loss.selfMask(), aoi, scale)
    gain_ha = _event_area_ha(gain.selfMask(), aoi, scale)
    maintained_ha = _event_area_ha(maintained.selfMask(), aoi, scale)

    class_img = (
        ee.Image(0)
        .where(maintained, 1)
        .where(loss, 2)
        .where(gain, 3)
        .selfMask()
        .rename("forest_change_class")
    )
    tile = get_tile_url(class_img, {"min": 1, "max": 3, "palette": ["#2e7d32", "#e53935", "#8bc34a"]}, "Forest Cover Change")

    return {
        "tile_url": tile["tile_url"] if tile else None,
        "statistics": {
            "pre_forest_area_ha": pre_ha,
            "post_forest_area_ha": post_ha,
            "difference_ha": round(post_ha - pre_ha, 2),
            "forest_loss_ha": loss_ha,
            "forest_gain_ha": gain_ha,
            "forest_maintained_ha": maintained_ha,
        },
        "legend": [
            {"label": "Forest Maintained", "color": "#2e7d32"},
            {"label": "Forest Loss", "color": "#e53935"},
            {"label": "Forest Gain", "color": "#8bc34a"},
        ],
        "confidence_summary": None,
        "features": None,
    }


# --- public cross-module alias ------------------------------------------------


def compute_flood_change(aoi, pre_date: dt.date, post_date: dt.date, scale: int = 30) -> dict:
    """Public alias of `_compute_flood_change` for cross-module callers
    (`crop_monitoring_service`'s Flood/Excess-Water Impact sub-analysis) -
    `_compute_flood_change` already takes a raw `ee.Geometry` + two dates, no
    persisted `DisasterAOI`/`AnalysisRun` needed to call it. Kept as a thin
    alias rather than renaming the underscore-prefixed original, to avoid
    touching every existing in-module call site above."""
    return _compute_flood_change(aoi, pre_date, post_date, scale)


# --- dispatcher --------------------------------------------------------------


def validate_inputs(db, event_id, aoi_id, pre_id, post_id, model_id):
    model = get_model(model_id)
    event = disaster_repo.get_event(db, event_id)
    aoi = disaster_repo.get_aoi(db, aoi_id)
    pre = disaster_repo.get_imagery(db, pre_id) if pre_id else None
    post = disaster_repo.get_imagery(db, post_id) if post_id else None
    if not model:
        raise AnalysisError("Model tidak terdaftar", 400)
    if event is None:
        raise AnalysisError("Disaster event tidak ditemukan", 404)
    if aoi is None or aoi.event_id != event_id:
        raise AnalysisError("AOI tidak tersedia atau berasal dari event lain", 400)
    if pre is not None and pre.event_id != event_id:
        raise AnalysisError("Imagery pre berasal dari event lain", 400)
    if post is not None and post.event_id != event_id:
        raise AnalysisError("Imagery post berasal dari event lain", 400)
    check = check_inputs(model_id, event, aoi, pre, post)
    if not check.allowed:
        raise AnalysisError("; ".join(check.reasons), 400)
    images = [pre, post]
    return model, aoi, images[0], images[1]


def run_analysis(db: Session, run_id: int, force: bool = False) -> dict:
    import hashlib
    import json
    from app.services.disaster_segmentation_service import compute_segmentation
    from app.services.wildfire_analysis_service import compute_persisted_product

    run = disaster_repo.get_run(db, run_id)
    if run is None:
        raise AnalysisError("Analysis run not found", 404)
    db.refresh(run, with_for_update=True)
    if run.status == "processing":
        raise AnalysisError("Analisis ini masih diproses", 409)
    try:
        model, aoi_row, pre_img, post_img = validate_inputs(db, run.event_id, run.aoi_id,
            run.pre_imagery_id, run.post_imagery_id, run.model_id)
        key = hashlib.sha256(json.dumps([run.event_id, run.aoi_id, run.pre_imagery_id,
            run.post_imagery_id, run.model_id, model["version"], aoi_row.geojson,
            getattr(pre_img, "acquisition_date", None), getattr(post_img, "acquisition_date", None),
            getattr(run, "parameters", None) or {}], sort_keys=True, default=str).encode()).hexdigest()
        for candidate in ([] if force else disaster_repo.list_runs_for_event(db, run.event_id)):
            if candidate.status not in ("completed", "review_required", "published"):
                continue
            cached = disaster_repo.get_result_for_run(db, candidate.id)
            if not cached or (candidate.id != run.id and not getattr(cached, "provenance", None)) or (cached.statistics or {}).get("cache_key") != key:
                continue
            # GEE map URLs are ephemeral: bound reuse, never pretend an old tile is fresh.
            if not candidate.completed_at or (dt.datetime.now(dt.UTC) - candidate.completed_at.replace(tzinfo=dt.UTC)).total_seconds() > 21600:
                continue
            if candidate.id != run.id:
                cached = disaster_repo.upsert_result(db, run.id, {
                    **cached.to_dict(include_features=True),
                    "statistics": cached.statistics,
                    "provenance": build_provenance(
                        disaster_repo.get_event(db, run.event_id), aoi_row, pre_img, post_img, model,
                        parameters=getattr(run, "parameters", None) or {},
                    ),
                    "validation_status": model_state(model),
                    "limitations": list(model.get("limitations", [])),
                })
                run.status = "review_required" if (cached.statistics or {}).get("comparison", {}).get("review_reasons") else "completed"
                run.error_message = None
                run.model_version = model["version"]
                run.completed_at = candidate.completed_at
                db.commit()
            return {"run": run.to_dict(), "result": cached.to_dict(include_features=True), "cached": True}
        run.status = "processing"
        run.error_message = None
        run.model_version = model["version"]
        run.started_at = dt.datetime.now(dt.UTC)
        db.commit()
        event = disaster_repo.get_event(db, run.event_id)
        aoi = create_geometry_from_payload({"geojson": aoi_row.geojson})
        pre_date = pre_img.acquisition_date if pre_img else None
        post_date = post_img.acquisition_date if post_img else None
        if run.model_id in {"fire_hotspot_observation_v1", "fire_burned_area_v1", "fire_dnbr_v1"}:
            output = compute_persisted_product(
                    db, event, aoi_row, pre_img, post_img, run.model_id, getattr(run, "parameters", None) or {}
            )
        elif run.model_id == "dynamic_world_v1":
            output = compute_segmentation(aoi, pre_date, post_date)
        elif run.model_id == "flood_change_v1":
            output = _compute_flood_change(aoi, pre_date, post_date)
        elif run.model_id == "water_segmentation_v1":
            output = _compute_water_segmentation(aoi, pre_date, post_date)
        elif run.model_id == "forest_change_v1":
            output = _compute_forest_change(aoi, pre_date, post_date)
        else:
            raise AnalysisError("Model belum memiliki implementasi", 400)
        requires_review = bool(output.pop("requires_review", False))
        if not output.get("statistics") or (
            not output.get("tile_url") and not output.get("features")
        ):
            raise AnalysisError("Model tidak menghasilkan output dan statistik yang valid", 422)
        output["statistics"]["cache_key"] = key
        output["provenance"] = build_provenance(
                event, aoi_row, pre_img, post_img, model, parameters=getattr(run, "parameters", None) or {}
        )
        output["validation_status"] = model_state(model)
        output["limitations"] = list(model.get("limitations", []))
        comparison = output["statistics"].get("comparison")
        if comparison:
            comparison.update(event_id=run.event_id, aoi_id=run.aoi_id,
                pre_imagery_id=run.pre_imagery_id, post_imagery_id=run.post_imagery_id)
        result = disaster_repo.upsert_result(db, run.id, output)
        run.status = "review_required" if requires_review or (comparison and comparison.get("review_reasons")) else "completed"
        run.completed_at = dt.datetime.now(dt.UTC)
        db.commit()
        return {"run": run.to_dict(), "result": result.to_dict(include_features=True)}
    except Exception as exc:
        logger.exception("Disaster analysis run %s model %s failed", run_id, run.model_id)
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = dt.datetime.now(dt.UTC)
        db.commit()
        if isinstance(exc, AnalysisError):
            raise
        raise AnalysisError(f"Analisis gagal: {exc}", 500) from exc
