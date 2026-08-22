"""Raw satellite imagery browser - list individual scenes with their real
acquisition date+time and view any single one as true-color RGB, with no
compositing across a date range and no land-cover/vegetation/carbon analysis
involved.

User request: "bagaimana bisa melihat citra satelit utk tanggal beserta jam
tertentu, tanpa harus land cover?" - every existing analysis module
(vegetation/carbon/landcover) always builds a MEDIAN/MODE composite over a
date range and only ever exposes month-level (or, for Dynamic World only,
day-level) granularity - none of them expose the actual per-scene
`system:time_start` (which carries a real time-of-day, e.g. Sentinel-2's
~03:15 UTC overpass) or let a user view one single acquisition unmodified.
This module fills that gap.

Unlike the analysis pipelines, scenes here are shown UNMASKED (clouds visible
as white, not removed) - the point of browsing is to let a user visually
judge a specific scene themselves, not to extract a clean statistic from it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import ee

from app.registries.satellite_provider_registry import resolve_satellite
from app.repositories.satellite_provider_repo import get_satellite_meta
from app.services.gee_common import AnalysisError, create_geometry_from_payload, get_tile_url, standardize_bands

logger = logging.getLogger(__name__)

# Scene-level cloud-percentage property name, per satellite family - same
# properties already relied on by vegetation_service.py's per-satellite
# collection filters (CLOUDY_PIXEL_PERCENTAGE for S2, CLOUD_COVER for Landsat).
_CLOUD_PROPERTY = {
    "sentinel2": "CLOUDY_PIXEL_PERCENTAGE",
    "landsat8": "CLOUD_COVER",
    "landsat9": "CLOUD_COVER",
}

# Caps how many scenes a single /imagery/scenes call can return - a wide date
# range over a big AOI could otherwise match hundreds of tiles' worth of
# scenes; this is a browsing tool, not a bulk export, so keep it cheap.
_MAX_SCENES = 200


def list_scenes(db, data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)

    aoi = create_geometry_from_payload(data["aoi"])
    satellite = resolve_satellite(data.get("satellite"))
    meta = get_satellite_meta(db, satellite)

    start_date = data["start_date"]
    end_date = data["end_date"]
    max_cloud_cover = data.get("max_cloud_cover")
    cloud_prop = _CLOUD_PROPERTY.get(satellite, "CLOUDY_PIXEL_PERCENTAGE")

    col = (
        ee.ImageCollection(meta["gee_collection"])
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .sort("system:time_start")
    )
    if max_cloud_cover is not None:
        col = col.filter(ee.Filter.lte(cloud_prop, float(max_cloud_cover)))
    col = col.limit(_MAX_SCENES)

    total_size = col.size().getInfo()
    if total_size == 0:
        return {"scenes": [], "count": 0, "satellite": meta, "truncated": False}

    # Single round-trip: bundle all 3 per-scene arrays into one server-side
    # dict so this is one getInfo() call, not one per scene (which would be
    # up to _MAX_SCENES separate network round trips).
    bundle = ee.Dictionary(
        {
            "ids": col.aggregate_array("system:index"),
            "times": col.aggregate_array("system:time_start"),
            "clouds": col.aggregate_array(cloud_prop),
        }
    ).getInfo()

    scenes = []
    for scene_id, ts, cloud in zip(bundle["ids"], bundle["times"], bundle["clouds"]):
        acquired = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        scenes.append(
            {
                "id": scene_id,
                # Real acquisition timestamp incl. time-of-day (UTC) - this is
                # the whole point of this endpoint vs. every composite-based one.
                "acquired_at": acquired.isoformat().replace("+00:00", "Z"),
                "cloud_cover_pct": round(float(cloud), 1) if cloud is not None else None,
            }
        )

    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": meta,
        "truncated": total_size >= _MAX_SCENES,
    }


def get_scene_tile(db, data: dict) -> dict:
    if not data.get("scene_id"):
        raise AnalysisError("scene_id is required", 400)

    satellite = resolve_satellite(data.get("satellite"))
    meta = get_satellite_meta(db, satellite)
    scene_id = data["scene_id"]
    matches = ee.ImageCollection(meta["gee_collection"]).filter(ee.Filter.eq("system:index", scene_id))

    # ee.ImageCollection.first() on a filter that matches nothing evaluates to
    # a server-side null, not a band-less-but-valid Image - calling anything
    # on it (e.g. .bandNames()) raises an opaque EEException ("Parameter
    # 'image' is required and may not be null") only once GEE evaluates it.
    # Check the FILTERED COLLECTION's size first instead, so a bad/stale
    # scene_id is a clean 404 (verified live: this is the only ordering that
    # doesn't throw before reaching this check).
    if matches.size().getInfo() == 0:
        raise AnalysisError(f"Scene '{scene_id}' tidak ditemukan untuk {meta['name']}", 404)
    img = matches.first()

    # Reflectance scaling only - deliberately NOT the same per-pixel cloud
    # mask analysis pipelines use (mask_landsat_clouds/build_s2_cloud_masked_collection):
    # this view is meant to show the scene as-is, clouds and all, so the user
    # can judge it visually - masking would hide exactly what they're checking for.
    if satellite == "sentinel2":
        img = img.divide(10000)
    else:  # landsat8 / landsat9 (Collection 2 Level-2 surface reflectance)
        img = img.select("SR_B.*").multiply(0.0000275).add(-0.2)

    img = standardize_bands(img, meta["band_role_map"])

    if data.get("aoi"):
        aoi = create_geometry_from_payload(data["aoi"])
        img = img.clip(aoi)

    # Same true-color visualization convention as every composite RGB tile in
    # this app (vegetation_service.py) - 0-0.3 reflectance stretch on B4/B3/B2.
    tile = get_tile_url(img, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3}, "RGB Scene")
    if not tile:
        raise AnalysisError("Gagal membuat tile untuk scene ini", 500)

    return {"scene_id": scene_id, "tile_url": tile["tile_url"], "satellite": meta}
