"""Analysis execution for the Disaster Intelligence Dashboard.

`run_analysis(db, run_id)` is the single entry point every admin "Run
Analysis" action calls. It loads the AnalysisRun + its AOI + pre/post
imagery rows (all already persisted by the admin AOI/imagery configuration
steps - nothing here draws its own AOI or queries arbitrary dates), dispatches
to one of the 3 real compute functions by `model_id`, and writes the result
via `disaster_repo.upsert_result`. Runs stay synchronous (blocking GEE calls,
same pattern as every other analyze_* service in this codebase - no job queue
exists here) - status only ever visibly transitions queued -> completed/failed
within one request.

Only 3 models are wired to real computation - see
`app/registries/disaster_model_registry.py` for why the other 4 are
`enabled: False`. Calling `run_analysis` for a disabled model_id raises
`AnalysisError` before touching GEE.
"""
from __future__ import annotations

import datetime as dt
import logging

import ee
from sqlalchemy.orm import Session

from app.registries.disaster_model_registry import get_model
from app.repositories import disaster_repo
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


def run_analysis(db: Session, run_id: int) -> dict:
    run = disaster_repo.get_run(db, run_id)
    if run is None:
        raise AnalysisError("Analysis run not found", 404)

    model = get_model(run.model_id)
    if model is None or not model["enabled"]:
        raise AnalysisError(f"Model '{run.model_id}' is not available to run", 400)

    aoi_row = disaster_repo.get_aoi(db, run.aoi_id)
    if aoi_row is None:
        raise AnalysisError("AOI not found for this run", 404)
    aoi = create_geometry_from_payload({"geojson": aoi_row.geojson})

    pre_img = disaster_repo.get_imagery(db, run.pre_imagery_id) if run.pre_imagery_id else None
    post_img = disaster_repo.get_imagery(db, run.post_imagery_id) if run.post_imagery_id else None
    pre_date = pre_img.acquisition_date if pre_img else None
    post_date = post_img.acquisition_date if post_img else None

    run.status = "processing"
    run.started_at = dt.datetime.now(dt.UTC)
    db.commit()

    try:
        if run.model_id == "flood_change_v1":
            if pre_date is None or post_date is None:
                raise AnalysisError("Flood Change membutuhkan imagery pre dan post", 400)
            output = _compute_flood_change(aoi, pre_date, post_date)
        elif run.model_id == "water_segmentation_v1":
            output = _compute_water_segmentation(aoi, pre_date, post_date)
        elif run.model_id == "forest_change_v1":
            if post_date is None:
                raise AnalysisError("Forest Cover Change membutuhkan minimal imagery post", 400)
            output = _compute_forest_change(aoi, pre_date, post_date)
        else:
            raise AnalysisError(f"Model '{run.model_id}' belum memiliki implementasi", 400)
    except AnalysisError as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = dt.datetime.now(dt.UTC)
        db.commit()
        raise
    except Exception as exc:
        logger.exception("Disaster analysis run %s failed", run_id)
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = dt.datetime.now(dt.UTC)
        db.commit()
        raise AnalysisError(str(exc), 500) from exc

    run.status = "completed"
    run.completed_at = dt.datetime.now(dt.UTC)
    db.commit()

    result = disaster_repo.upsert_result(db, run.id, output)
    return {"run": run.to_dict(), "result": result.to_dict(include_features=True)}
