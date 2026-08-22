"""
stac_provider.py — Microsoft Planetary Computer STAC ingestion, non-GEE.

Returns numpy band-stacks (NonGeeStack) via GridSpec/load_raster_to_grid
from providers/local_raster_provider.py — never ee.Image.

Requires: pystac-client, planetary-computer (pip install pystac-client
planetary-computer). Everything else (rasterio, numpy) already installed.
"""
from __future__ import annotations

import logging

import numpy as np

from app.providers.feature_engineering_non_gee import NonGeeStack
from app.providers.local_raster_provider import GridSpec, load_raster_to_grid

logger = logging.getLogger(__name__)

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

S2_ASSET_MAP = {
    "B2": "B02", "B3": "B03", "B4": "B04", "B5": "B05", "B6": "B06",
    "B7": "B07", "B8": "B08", "B8A": "B8A", "B11": "B11", "B12": "B12",
}
S2_SCALE = 10000.0  # Sentinel-2 L2A DN -> reflectance (divide by 10000)

# SCL classes to mask: 3=cloud shadow, 8/9=cloud medium/high prob, 10=thin cirrus
# (matches CarbonInferenceEngine._mask_s2_clouds exactly)
SCL_MASK_CLASSES = (3, 8, 9, 10)

BBox = tuple[float, float, float, float]


def _stac_client():
    from pystac_client import Client
    return Client.open(PC_STAC_URL)


def search_sentinel2_items(
    bbox_lonlat: BBox, start_date: str, end_date: str, max_cloud_cover: int = 60,
) -> list:
    """Search Planetary Computer's sentinel-2-l2a collection.

    Signing is deferred to read time (planetary_computer.sign() at
    load_raster_to_grid call) since signed URLs expire — don't sign upfront.
    """
    client = _stac_client()
    search = client.search(
        collections=["sentinel-2-l2a"],
        bbox=list(bbox_lonlat),
        datetime=f"{start_date}/{end_date}",
        query={"eo:cloud_cover": {"lt": max_cloud_cover}},
    )
    items = list(search.items())
    logger.info(f"STAC search sentinel-2-l2a {bbox_lonlat} {start_date}/{end_date}: {len(items)} items")
    return items


def _signed_href(item, asset_key: str) -> str:
    import planetary_computer
    asset = item.assets[asset_key]
    signed = planetary_computer.sign(asset)
    return signed.href


def _band_scale_offset(item, asset_key: str) -> tuple[float, float]:
    """Reflectance = DN * scale + offset.

    Sentinel-2 L2A processing baseline >= 04.00 (all items acquired since
    2022-01-25) adds a BOA_ADD_OFFSET of -1000 DN before the x0.0001 scale,
    so DN=0 no longer means reflectance=0. Read the authoritative per-band
    scale/offset from the STAC item's `raster:bands` extension when present;
    otherwise fall back to the processing-baseline heuristic. Missing this
    was caught by the tiny-Java-bbox smoke test (composite max ~2.0 instead
    of ~1.0 before this fix).
    """
    asset = item.assets.get(asset_key)
    raster_bands = (asset.extra_fields.get("raster:bands") if asset else None) or []
    if raster_bands:
        band0 = raster_bands[0]
        scale = float(band0.get("scale", 0.0001))
        offset = float(band0.get("offset", 0.0))
        return scale, offset

    scale = 1.0 / S2_SCALE
    baseline = item.properties.get("s2:processing_baseline", "00.00")
    try:
        major = float(baseline.split(".")[0])
    except (ValueError, AttributeError):
        major = 0.0
    offset = -1000 * scale if major >= 4.0 else 0.0
    return scale, offset


def build_s2_cloud_masked_composite(
    items: list,
    grid: GridSpec,
    bands: list[str] | None = None,
    composite: str = "median",
    percentile: int | None = None,
    max_items: int | None = None,
) -> tuple[dict[str, np.ndarray], dict]:
    """Cloud-masked composite (median or percentile) across STAC items.

    Reads each item's SCL asset first (nearest resampling — categorical),
    builds a cloud/shadow/cirrus mask, then reads each spectral band and
    applies the mask before stacking across time.

    Returns (bands dict {band_name: (H,W) array in 0-1 reflectance},
    stats dict with n_items_used / n_valid_pixels_per_band for QA).
    Raises ValueError if 0 items or an all-NaN composite result.
    """
    if bands is None:
        bands = list(S2_ASSET_MAP.keys())
    if max_items is not None:
        items = items[:max_items]
    if not items:
        raise ValueError("build_s2_cloud_masked_composite: 0 STAC items provided")

    per_band_stacks: dict[str, list[np.ndarray]] = {b: [] for b in bands}
    n_items_used = 0

    for item in items:
        try:
            scl_href = _signed_href(item, "SCL")
        except KeyError:
            logger.warning(f"Item {item.id} has no SCL asset, skipping cloud mask for this item")
            scl_href = None

        cloud_mask = None
        if scl_href is not None:
            scl = load_raster_to_grid(scl_href, grid, resampling="nearest")
            if scl is not None:
                cloud_mask = np.isin(scl, SCL_MASK_CLASSES)

        item_had_valid_band = False
        for band_name in bands:
            asset_key = S2_ASSET_MAP[band_name]
            try:
                href = _signed_href(item, asset_key)
            except KeyError:
                continue
            arr = load_raster_to_grid(href, grid, resampling="bilinear")
            if arr is None:
                continue
            scale, offset = _band_scale_offset(item, asset_key)
            arr = arr * scale + offset
            if cloud_mask is not None:
                arr = np.where(cloud_mask, np.nan, arr)
            per_band_stacks[band_name].append(arr)
            item_had_valid_band = True

        if item_had_valid_band:
            n_items_used += 1

    if n_items_used == 0:
        raise ValueError(
            "build_s2_cloud_masked_composite: 0 items produced any readable band "
            "(network/signing failures for every item) — check STAC search results and connectivity"
        )

    composited: dict[str, np.ndarray] = {}
    n_valid_pixels: dict[str, int] = {}
    for band_name, stack_list in per_band_stacks.items():
        if not stack_list:
            raise ValueError(f"build_s2_cloud_masked_composite: no valid data for band '{band_name}' across {n_items_used} items")
        stacked = np.stack(stack_list, axis=0)
        with np.errstate(invalid="ignore"):
            if composite == "median":
                comp = np.nanmedian(stacked, axis=0)
            elif composite == "percentile":
                if percentile is None:
                    raise ValueError("composite='percentile' requires percentile=<int>")
                comp = np.nanpercentile(stacked, percentile, axis=0)
            else:
                raise ValueError(f"Unknown composite method '{composite}'")
        composited[band_name] = comp
        n_valid_pixels[band_name] = int(np.sum(~np.isnan(comp)))

    if all(v == 0 for v in n_valid_pixels.values()):
        raise ValueError("build_s2_cloud_masked_composite: composite is all-NaN for every band")

    stats = {
        "n_items_searched": len(items),
        "n_items_used": n_items_used,
        "n_valid_pixels_per_band": n_valid_pixels,
        "composite_method": composite if composite != "percentile" else f"p{percentile}",
    }
    return composited, stats


def build_dem_derivatives(grid: GridSpec) -> dict[str, np.ndarray]:
    """Mosaic Copernicus DEM GLO-30 tiles covering grid.bbox_lonlat, then
    compute slope/aspect via feature_engineering_non_gee.compute_terrain
    (numpy.gradient approximation, documented simplification vs ee.Terrain).
    """
    from training.feature_engineering_non_gee import compute_terrain

    client = _stac_client()
    search = client.search(collections=["cop-dem-glo-30"], bbox=list(grid.bbox_lonlat))
    items = list(search.items())
    if not items:
        raise ValueError(f"build_dem_derivatives: no cop-dem-glo-30 items found for bbox {grid.bbox_lonlat}")

    accumulator = np.full(grid.shape, np.nan, dtype=np.float32)
    n_used = 0
    for item in items:
        try:
            href = _signed_href(item, "data")
        except KeyError:
            continue
        tile = load_raster_to_grid(href, grid, resampling="bilinear")
        if tile is None:
            continue
        valid = ~np.isnan(tile)
        need_fill = np.isnan(accumulator) & valid
        accumulator = np.where(need_fill, tile, accumulator)
        n_used += 1

    if n_used == 0:
        raise ValueError("build_dem_derivatives: 0 DEM tiles readable")

    elevation = accumulator
    terrain = compute_terrain(elevation, pixel_size_m=grid.resolution_m)
    terrain["dem_n_items_used"] = n_used
    return terrain


def build_worldcover(grid: GridSpec, year: int = 2021) -> dict:
    """Mosaic ESA WorldCover tiles. Only 2020 (v100) and 2021 (v200) epochs
    exist — pins to whichever is nearest to `year`, records landcover_year.
    """
    client = _stac_client()
    available_years = {2020: "esa-worldcover", 2021: "esa-worldcover"}
    target_year = min(available_years.keys(), key=lambda y: abs(y - year))

    search = client.search(
        collections=["esa-worldcover"], bbox=list(grid.bbox_lonlat),
        datetime=f"{target_year}-01-01/{target_year}-12-31",
    )
    items = list(search.items())
    if not items:
        raise ValueError(f"build_worldcover: no esa-worldcover items found for bbox {grid.bbox_lonlat}, year {target_year}")

    accumulator = np.full(grid.shape, np.nan, dtype=np.float32)
    n_used = 0
    for item in items:
        try:
            href = _signed_href(item, "map")
        except KeyError:
            continue
        tile = load_raster_to_grid(href, grid, resampling="nearest")
        if tile is None:
            continue
        valid = ~np.isnan(tile)
        need_fill = np.isnan(accumulator) & valid
        accumulator = np.where(need_fill, tile, accumulator)
        n_used += 1

    if n_used == 0:
        raise ValueError("build_worldcover: 0 WorldCover tiles readable")

    landcover = accumulator
    forest_mask = (landcover == 10).astype(np.float32)  # WorldCover class 10 = Tree cover
    return {"landcover": landcover, "forest_mask": forest_mask, "landcover_year": target_year, "worldcover_n_items_used": n_used}


def build_s1_composite(grid: GridSpec, start_date: str, end_date: str) -> dict | None:
    """Best-effort Sentinel-1 RTC composite. Returns None (not a hard crash)
    on any failure — S1 is optional per the task brief; caller must drop it
    from the feature stack on a None return."""
    try:
        client = _stac_client()
        search = client.search(
            collections=["sentinel-1-rtc"], bbox=list(grid.bbox_lonlat),
            datetime=f"{start_date}/{end_date}",
        )
        items = list(search.items())
        if not items:
            logger.info("build_s1_composite: no sentinel-1-rtc items found, S1 unavailable")
            return None

        vv_stack, vh_stack = [], []
        for item in items:
            try:
                vv = load_raster_to_grid(_signed_href(item, "vv"), grid, resampling="bilinear")
                vh = load_raster_to_grid(_signed_href(item, "vh"), grid, resampling="bilinear")
            except KeyError:
                continue
            if vv is not None:
                vv_stack.append(vv)
            if vh is not None:
                vh_stack.append(vh)

        if not vv_stack or not vh_stack:
            logger.info("build_s1_composite: no readable VV/VH assets, S1 unavailable")
            return None

        with np.errstate(invalid="ignore"):
            vv_med = np.nanmedian(np.stack(vv_stack), axis=0)
            vh_med = np.nanmedian(np.stack(vh_stack), axis=0)

        return {
            "VV": vv_med, "VH": vh_med,
            "VV_minus_VH": vv_med - vh_med,
            "s1_n_items_used": len(items),
        }
    except Exception as exc:  # noqa: BLE001 - S1 is a best-effort feature; the caller proceeds without it
        logger.warning(f"build_s1_composite: best-effort S1 fetch failed, dropping S1 from stack: {exc}")
        return None


def build_full_stack(
    bbox_lonlat: BBox,
    start_date: str,
    end_date: str,
    resolution_m: float = 20,
    include_s1: bool = True,
    include_dem: bool = True,
    include_landcover: bool = True,
    composite: str = "median",
    max_cloud_cover: int = 60,
    max_items: int | None = None,
) -> NonGeeStack:
    """Single public entry point: builds a complete NonGeeStack from
    Planetary Computer STAC for the given AOI/date range."""
    grid = GridSpec(bbox_lonlat, resolution_m=resolution_m)

    items = search_sentinel2_items(bbox_lonlat, start_date, end_date, max_cloud_cover)
    if not items:
        raise ValueError(
            f"build_full_stack: 0 sentinel-2-l2a items for bbox={bbox_lonlat} "
            f"date_range={start_date}/{end_date} cloud<{max_cloud_cover}. Try widening the date range or cloud threshold."
        )

    s2_bands, s2_stats = build_s2_cloud_masked_composite(
        items, grid, composite=composite, max_items=max_items,
    )

    bands: dict[str, np.ndarray] = dict(s2_bands)
    provenance: dict = {
        "provider": "non_gee_stac",
        "stac_catalog": "planetary-computer",
        "s2_collection": "sentinel-2-l2a",
        "date_range": [start_date, end_date],
        "resolution_m": resolution_m,
        "composite_method": s2_stats["composite_method"],
        "n_stac_items_s2": s2_stats["n_items_used"],
        "s1_included": False,
        "s1_collection": None,
        "dem_collection": None,
        "landcover_collection": None,
        "landcover_year": None,
    }

    if include_dem:
        terrain = build_dem_derivatives(grid)
        bands["elevation"] = terrain["elevation"]
        bands["slope"] = terrain["slope"]
        bands["aspect"] = terrain["aspect"]
        provenance["dem_collection"] = "cop-dem-glo-30"

    if include_landcover:
        wc = build_worldcover(grid)
        bands["landcover"] = wc["landcover"]
        bands["forest_mask"] = wc["forest_mask"]
        provenance["landcover_collection"] = "esa-worldcover"
        provenance["landcover_year"] = wc["landcover_year"]

    if include_s1:
        s1 = build_s1_composite(grid, start_date, end_date)
        if s1 is not None:
            bands["VV"] = s1["VV"]
            bands["VH"] = s1["VH"]
            bands["VV_minus_VH"] = s1["VV_minus_VH"]
            provenance["s1_included"] = True
            provenance["s1_collection"] = "sentinel-1-rtc"

    return NonGeeStack(bands=bands, transform=grid.transform, crs=grid.crs, shape=grid.shape, provenance=provenance)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Smoke test on a TINY Central-Java bbox, short date range, per build-sequence
    # step 4: validate STAC reachability/index correctness before scaling up.
    tiny_bbox = (110.3, -7.6, 110.6, -7.3)
    print(f"Smoke test: searching sentinel-2-l2a over tiny bbox {tiny_bbox}, 2022-06 to 2022-08 ...")
    items = search_sentinel2_items(tiny_bbox, "2022-06-01", "2022-08-31", max_cloud_cover=60)
    assert len(items) >= 1, "expected at least 1 Sentinel-2 item over Central Java in a 3-month window"
    print(f"Found {len(items)} items")

    grid = GridSpec(tiny_bbox, resolution_m=20)
    print(f"Grid: shape={grid.shape}, crs={grid.crs}")

    bands, stats = build_s2_cloud_masked_composite(items, grid, max_items=6)
    print(f"Composite stats: {stats}")
    for b, arr in bands.items():
        valid = arr[~np.isnan(arr)]
        if valid.size:
            print(f"  {b}: min={valid.min():.4f} max={valid.max():.4f} mean={valid.mean():.4f} n_valid={valid.size}")
    b8 = bands["B8"]
    b4 = bands["B4"]
    finite = ~np.isnan(b8) & ~np.isnan(b4)
    assert finite.sum() > 0, "expected some finite pixels in composite"
    # Median reflectance must be sane; a small tail of SCL-class-1
    # (saturated/defective, unmasked — same limitation as the existing GEE
    # pipeline's _mask_s2_clouds) can still spike above 1.0 in individual
    # pixels even after compositing, especially with few images.
    median_b8 = float(np.nanmedian(b8[finite]))
    assert 0.0 <= median_b8 <= 0.7, f"median B8 reflectance {median_b8:.4f} outside plausible range -- scale/DN bug"
    frac_over_1 = float(np.mean(b8[finite] > 1.0))
    assert frac_over_1 < 0.05, f"{frac_over_1:.1%} of pixels >1.0 reflectance -- too many for SCL-class-1 tail, check offset"
    print(f"Reflectance sanity OK: median B8={median_b8:.4f}, {frac_over_1:.2%} pixels >1.0 (expected: small SCL class-1 tail)")

    print("STAC smoke test passed (network-dependent — rerun if Planetary Computer is unreachable).")
