"""Raw satellite imagery browser - list individual scenes with their real
acquisition date+time and view any single one visualized appropriately for
its sensor type, with no compositing across a date range and no land-cover/
vegetation/carbon analysis involved.

User request: "bagaimana bisa melihat citra satelit utk tanggal beserta jam
tertentu, tanpa harus land cover?" - every existing analysis module
(vegetation/carbon/landcover) always builds a MEDIAN/MODE composite over a
date range and only ever exposes month-level (or, for Dynamic World only,
day-level) granularity - none of them expose the actual per-scene
`system:time_start` (which carries a real time-of-day, e.g. Sentinel-2's
~03:15 UTC overpass) or let a user view one single acquisition unmodified.
Later extended (user request) to more than just optical Sentinel-2/Landsat:
Sentinel-1 SAR and Sentinel-5P atmospheric-gas products, each needing a
genuinely different visualization strategy - see imagery_provider_registry.py
for the full rationale on why that's a separate catalog from
satellite_provider_registry.py (vegetation/carbon's index-analysis one).

Unlike the analysis pipelines, optical scenes here are shown UNMASKED (clouds
visible as white, not removed) - the point of browsing is to let a user
visually judge a specific scene themselves, not to extract a clean statistic
from it. SAR is naturally cloud-independent; S5P products have no per-pixel
cloud mask applied here either (their own `cloud_fraction`/similar bands are
metadata, not something this endpoint filters on beyond the optional
`max_cloud_cover` scene-level query for optical sensors).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import ee

from app.registries.imagery_provider_registry import (
    IMAGERY_PROVIDERS,
    get_imagery_provider_meta,
    resolve_imagery_provider,
)
from app.services.gee_common import (
    AnalysisError,
    create_geometry_from_payload,
    get_tile_url,
    resolve_cloud_mask_technique,
)

logger = logging.getLogger(__name__)

# Caps how many scenes a single /imagery/scenes call can return - a wide date
# range over a big AOI could otherwise match hundreds of tiles' worth of
# scenes; this is a browsing tool, not a bulk export, so keep it cheap.
_MAX_SCENES = 200


def list_providers() -> dict:
    """GET /imagery/providers - static (no GEE) catalog for the satellite picker."""
    return {"providers": IMAGERY_PROVIDERS, "default": "sentinel2"}


def list_scenes(data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)

    aoi = create_geometry_from_payload(data["aoi"])
    provider_key = resolve_imagery_provider(data.get("satellite"))
    meta = get_imagery_provider_meta(provider_key)

    start_date = data["start_date"]
    end_date = data["end_date"]
    max_cloud_cover = data.get("max_cloud_cover")
    cloud_prop = meta.get("cloud_property")

    col = (
        ee.ImageCollection(meta["gee_collection"])
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .sort("system:time_start")
    )
    if cloud_prop and max_cloud_cover is not None:
        col = col.filter(ee.Filter.lte(cloud_prop, float(max_cloud_cover)))
    col = col.limit(_MAX_SCENES)

    total_size = col.size().getInfo()
    if total_size == 0:
        return {"scenes": [], "count": 0, "satellite": meta, "truncated": False}

    # Single round-trip: bundle all per-scene arrays into one server-side
    # dict so this is one getInfo() call, not one per scene.
    to_aggregate = {"ids": col.aggregate_array("system:index"), "times": col.aggregate_array("system:time_start")}
    if cloud_prop:
        to_aggregate["clouds"] = col.aggregate_array(cloud_prop)
    bundle = ee.Dictionary(to_aggregate).getInfo()

    ids = bundle["ids"]
    times = bundle["times"]
    clouds = bundle.get("clouds") if cloud_prop else [None] * len(ids)

    scenes = []
    for scene_id, ts, cloud in zip(ids, times, clouds):
        acquired = datetime.fromtimestamp(ts / 1000, tz=UTC)
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


def _apply_s2_single_scene_mask(img: ee.Image, technique: str) -> ee.Image:
    """Per-pixel cloud mask for ONE Sentinel-2 scene (not a collection
    composite) - user request: let the scene browser optionally mask clouds
    using the same SCL/QA60/s2cloudless techniques already available for
    Vegetation/Carbon (gee_common.build_s2_cloud_masked_collection), instead
    of only ever showing the raw unmasked scene. Kept separate from that
    function since these operate on a single ee.Image, not an
    ee.ImageCollection - the underlying per-technique logic is the same.

    "scl" is only valid for the L2A (Surface Reflectance) provider - the L1C/
    TOA variant has no SCL band at all (verified live) - callers must not
    offer "scl" for that provider (see IMAGERY_PROVIDERS[...]["cloud_mask_techniques"]).
    """
    if technique == "qa60":
        qa = img.select("QA60")
        cloud = qa.bitwiseAnd(1 << 10).neq(0)
        cirrus = qa.bitwiseAnd(1 << 11).neq(0)
        return img.updateMask(cloud.Or(cirrus).Not())
    if technique == "s2cloudless":
        prob_img = (
            ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY")
            .filter(ee.Filter.eq("system:index", img.get("system:index")))
            .first()
        )
        prob = ee.Image(prob_img).select("probability")
        return img.updateMask(prob.lt(40))
    # "scl" (default)
    scl = img.select("SCL")
    mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    return img.updateMask(mask)


def get_scene_tile(data: dict) -> dict:
    if not data.get("scene_id"):
        raise AnalysisError("scene_id is required", 400)

    provider_key = resolve_imagery_provider(data.get("satellite"))
    meta = get_imagery_provider_meta(provider_key)
    scene_id = data["scene_id"]

    matches = ee.ImageCollection(meta["gee_collection"]).filter(ee.Filter.eq("system:index", scene_id))
    # ee.ImageCollection.first() on a filter that matches nothing evaluates to
    # a server-side null, not a band-less-but-valid Image - calling anything
    # on it raises an opaque EEException only once GEE evaluates it. Check
    # the FILTERED COLLECTION's size first instead, so a bad/stale scene_id
    # is a clean 404 (verified live against this exact failure mode).
    if matches.size().getInfo() == 0:
        raise AnalysisError(f"Scene '{scene_id}' tidak ditemukan untuk {meta['name']}", 404)
    img = matches.first()

    visualization = meta["visualization"]

    if visualization == "rgb":
        # Optional per-pixel cloud mask (Sentinel-2 only, both L2A/L1C
        # variants) - user request: offer the same SCL/QA60/s2cloudless
        # techniques already used for Vegetation/Carbon, as an opt-in on top
        # of the default raw/unmasked view. Must run BEFORE .select(bands)
        # below, which drops every band not in band_role_map (SCL/QA60 included).
        available_techniques = meta.get("cloud_mask_techniques")
        requested_technique = data.get("cloud_mask_technique")
        if available_techniques and requested_technique:
            technique = resolve_cloud_mask_technique(requested_technique)
            if technique not in available_techniques:
                technique = available_techniques[0]
            img = _apply_s2_single_scene_mask(img, technique)

        band_role_map = meta["band_role_map"]
        if "reflectance_scale" in meta:
            bands = list(band_role_map.values())
            img = img.select(bands).multiply(meta["reflectance_scale"]).add(meta.get("reflectance_offset", 0))
        vis = {
            "bands": [band_role_map["red"], band_role_map["green"], band_role_map["blue"]],
            "min": meta["vis_min"],
            "max": meta["vis_max"],
        }
    elif visualization == "sar":
        sar_mode = data.get("sar_mode", "grayscale")  # "grayscale" | "composite"
        vv_band = meta["sar_bands"]["vv"]
        vh_band = meta["sar_bands"]["vh"]
        if sar_mode == "composite":
            ratio = img.select(vv_band).subtract(img.select(vh_band)).rename("ratio")
            img = ee.Image.cat([img.select(vv_band), img.select(vh_band), ratio])
            cv = meta["sar_composite_vis"]
            vis = {"bands": [vv_band, vh_band, "ratio"], "min": cv["min"], "max": cv["max"]}
        else:
            gv = meta["sar_grayscale_vis"]
            vis = {"bands": [gv["band"]], "min": gv["min"], "max": gv["max"]}
    elif visualization == "single_band":
        vis = {"bands": [meta["band"]], "min": meta["vis_min"], "max": meta["vis_max"], "palette": meta["palette"]}
    else:  # pragma: no cover - guarded by the registry itself, defensive only
        raise AnalysisError(f"Unknown visualization strategy '{visualization}' for {meta['name']}", 500)

    if data.get("aoi"):
        aoi = create_geometry_from_payload(data["aoi"])
        img = img.clip(aoi)

    tile = get_tile_url(img, vis, f"{meta['name']} scene")
    if not tile:
        raise AnalysisError("Gagal membuat tile untuk scene ini", 500)

    return {"scene_id": scene_id, "tile_url": tile["tile_url"], "satellite": meta}
