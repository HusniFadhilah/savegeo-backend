"""Vegetation index analysis service (GEE-heavy).

Ported (business logic unchanged) from legacy `backend/app.py`:
  - classify_vegetation_image / summarize_vegetation_classes /
    vegetation_classification_tile / compute_index_histogram: lines ~1257-1336
  - _s2_composite_for_period / _index_stats / _trend_and_anomalies: lines ~3370-3424
  - analyze_vegetation route body: lines ~3431-3556
  - analyze_vegetation_compare route body: lines ~3556-3625
  - analyze_timeseries route body: lines ~4053-4094

Metadata-only helpers (catalog, narrative generation) live in
`app.registries.vegetation_index_registry` and are imported here, not redefined.
"""
from __future__ import annotations

import logging

import ee

from app.core.config import get_settings
from app.registries.satellite_provider_registry import resolve_satellite
from app.registries.vegetation_index_registry import (
    VEGETATION_INDEX_CATALOG as VEGETATION_INDICES,
)
from app.registries.vegetation_index_registry import (
    available_bands_for_index,
    generate_comparison_narrative,
    generate_index_narrative,
)
from app.repositories.satellite_provider_repo import get_satellite_meta
from app.services import config_service
from app.services.gee_common import (
    AnalysisError,
    build_date_range,
    build_s2_cloud_masked_collection,
    calculate_index,
    create_geometry_from_payload,
    get_tile_url,
    mask_landsat_clouds,
    resolve_cloud_mask_technique,
    standardize_bands,
)

logger = logging.getLogger(__name__)


def _no_imagery_suggestion(meta: dict, year: int) -> str:
    """Better error hint for a zero-image composite. Audit finding: "S2 SR vs
    TOA provenance for 2015" - COPERNICUS/S2_SR_HARMONIZED's real global data
    floor is 2017 (verified live: 2015/2016 return zero images for every AOI
    tried), well after the satellite's 2015 launch, and the same gap applies
    to any other provider's `start_year`. A plain "try different dates" hint
    doesn't tell the user *why* an early year fails; naming the actual floor
    does."""
    start_year = meta.get("start_year")
    name = meta.get("name", "satelit ini")
    if start_year is not None and year < start_year:
        return (
            f"Tahun {year} lebih awal dari data {name} yang tersedia (mulai {start_year}). "
            f"Coba tahun {start_year} atau lebih baru, atau ganti provider satelit."
        )
    return "Coba ubah rentang tanggal, cloud threshold, atau ganti provider satelit"


# ─────────────────────────────────────────────
# Vegetation index classification / histogram helpers
# ─────────────────────────────────────────────

def classify_vegetation_image(index_image, index_name: str):
    """Bin a continuous index image into the registry's default classes.

    Cascades ee.Image.where() from the highest bin down to the lowest so each
    pixel ends up in the smallest class whose upper bound it is below (last
    bin, max=None, is the default/fallback value).
    """
    bins = VEGETATION_INDICES[index_name]["classification"]
    classified = ee.Image(len(bins) - 1).rename("class")
    for i in range(len(bins) - 2, -1, -1):
        threshold = bins[i]["max"]
        classified = classified.where(index_image.lt(threshold), i)
    return classified.updateMask(index_image.mask())


def summarize_vegetation_classes(index_image, index_name: str, aoi, scale: int):
    """Area (ha) + percentage per default classification bin, same grouped-reduce
    pattern as landcover_service.summarize_landcover_classes but keyed off the
    vegetation registry instead of LAND_COVER_LEGENDS."""
    settings = get_settings()
    bins = VEGETATION_INDICES[index_name]["classification"]
    class_band = classify_vegetation_image(index_image, index_name).toInt16()
    area_image = ee.Image.pixelArea().divide(10000).rename("area_ha").updateMask(class_band.mask())
    grouped = area_image.addBands(class_band).reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class_value"),
        geometry=aoi,
        scale=scale,
        maxPixels=int(settings.max_pixels),
        bestEffort=True,
        tileScale=4,
    ).getInfo()

    valid_items = []
    for item in (grouped or {}).get("groups", []):
        class_val = int(float(item.get("class_value")))
        area_ha = float(item.get("sum", 0) or 0)
        if 0 <= class_val < len(bins) and area_ha > 0:
            valid_items.append((class_val, area_ha))

    total_area_ha = sum(area_ha for _, area_ha in valid_items)
    if not total_area_ha:
        return None

    classes = {}
    for class_val, area_ha in valid_items:
        b = bins[class_val]
        classes[b["label"]] = {
            "area": round(area_ha, 2),
            "percentage": round(area_ha / total_area_ha * 100, 1),
            "color": b["color"],
            "class_value": class_val,
        }
    return {"classes": classes, "total_area_ha": round(total_area_ha, 2)}


def vegetation_classification_tile(index_image, index_name: str, layer_name: str):
    bins = VEGETATION_INDICES[index_name]["classification"]
    palette = [b["color"].replace("#", "") for b in bins]
    class_band = classify_vegetation_image(index_image, index_name)
    tile = get_tile_url(class_band, {"min": 0, "max": len(bins) - 1, "palette": palette}, f"{layer_name}_class")
    return tile["tile_url"] if tile else None


def compute_index_histogram(index_image, aoi, scale: int, min_val: float, max_val: float, buckets: int = 20):
    """Fixed-width histogram of pixel values for the distribution chart."""
    settings = get_settings()
    try:
        hist = index_image.reduceRegion(
            reducer=ee.Reducer.fixedHistogram(min_val, max_val, buckets),
            geometry=aoi, scale=scale, maxPixels=int(settings.max_pixels), bestEffort=True, tileScale=4,
        ).getInfo()
        band_name = index_image.bandNames().get(0).getInfo()
        rows = hist.get(band_name) if isinstance(hist, dict) else None
        if not rows:
            return []
        return [{"bin_start": round(row[0], 4), "bin_end": round(row[0] + (max_val - min_val) / buckets, 4), "count": int(row[1])} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Histogram computation failed: {e}")
        return []


def _composite_for_period(
    db, aoi, year, start_month, end_month, cloud_threshold, satellite: str = "sentinel2",
    cloud_mask_technique: str = "scl",
):
    """Build a cloud-masked median composite for whichever satellite provider
    was requested, then standardize its bands to the canonical Sentinel-2-style
    aliases so every downstream index formula stays sensor-agnostic.

    `gee_collection`/`band_role_map` come from get_satellite_meta(), which
    merges in any DB override (satellite_providers table) - only the
    sentinel2-vs-landsat cloud-mask *algorithm* choice below stays keyed off
    the resolved provider key itself (a DB row can't redefine that).

    `cloud_mask_technique` ("scl" | "qa60" | "s2cloudless", see gee_common.py)
    only applies to the Sentinel-2 path - Landsat keeps its own QA_PIXEL mask,
    there's no equivalent s2cloudless-style product for it in this catalog."""
    start_date, end_date = build_date_range(year, start_month, end_month)
    resolved_key = resolve_satellite(satellite)
    provider = get_satellite_meta(db, satellite)
    collection_id = provider["gee_collection"]

    if resolved_key == "sentinel2":
        collection = build_s2_cloud_masked_collection(
            aoi, start_date, end_date, cloud_threshold,
            technique=cloud_mask_technique, collection_id=collection_id,
        ).map(lambda img: img.divide(10000))
    else:  # landsat8 / landsat9 (Collection 2 Level-2 surface reflectance)
        collection = (
            ee.ImageCollection(collection_id)
            .filterBounds(aoi).filterDate(start_date, end_date)
            .filter(ee.Filter.lte("CLOUD_COVER", cloud_threshold))
            .map(mask_landsat_clouds)
        )

    size = collection.size().getInfo()
    if size == 0:
        return None, None, start_date, end_date, 0, None
    composite = standardize_bands(collection.median(), provider["band_role_map"]).clip(aoi)

    # Data-quality signal: % of AOI actually covered by a cloud-free pixel in
    # this composite (same technique as CarbonInferenceEngine._safe_s2_composite).
    valid_pixel_pct = None
    try:
        first_band = composite.bandNames().get(0)
        coverage = composite.select([first_band]).mask().reduceRegion(
            reducer=ee.Reducer.mean(), geometry=aoi, scale=100, bestEffort=True, maxPixels=int(1e8), tileScale=4,
        ).getInfo()
        valid_pixel_pct = round(float(next(iter(coverage.values())) or 0) * 100, 1) if coverage else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Valid-pixel coverage check failed: {e}")

    return composite, collection, start_date, end_date, size, valid_pixel_pct


def _index_stats(index_image, idx, aoi, veg_scale):
    settings = get_settings()
    stats = index_image.reduceRegion(
        reducer=ee.Reducer.minMax().combine(ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True), sharedInputs=True),
        geometry=aoi, scale=veg_scale, maxPixels=int(settings.max_pixels), bestEffort=True
    ).getInfo()
    return {
        "min": stats.get(f"{idx}_min"), "mean": stats.get(f"{idx}_mean"),
        "max": stats.get(f"{idx}_max"), "std_dev": stats.get(f"{idx}_stdDev"),
    }


def _trend_and_anomalies(series_means: list) -> dict:
    """Simple linear-slope trend + z-score anomaly flags for a time series of means."""
    values = [v for v in series_means if v is not None]
    n = len(values)
    if n < 2:
        return {"direction": "insufficient_data", "slope": None, "anomaly_indices": []}

    xs = list(range(n))
    mean_x, mean_y = sum(xs) / n, sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom if denom else 0

    std = (sum((y - mean_y) ** 2 for y in values) / n) ** 0.5
    anomaly_indices = []
    if std > 1e-9:
        for i, v in enumerate(series_means):
            if v is not None and abs((v - mean_y) / std) > 1.5:
                anomaly_indices.append(i)

    if abs(slope) < 1e-4:
        direction = "stabil"
    elif slope > 0:
        direction = "naik"
    else:
        direction = "turun"

    return {"direction": direction, "slope": round(slope, 5), "anomaly_indices": anomaly_indices}


# ─────────────────────────────────────────────
# Route-body logic
# ─────────────────────────────────────────────

def analyze_vegetation(db, data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    year = int(data.get("year", 2022))
    start_month = int(data.get("start_month", 6))
    end_month = int(data.get("end_month", 9))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    indices = data.get("indices", ["NDVI"])
    veg_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["veg_scale"]))
    want_classify = bool(data.get("classify", True))
    want_histogram = bool(data.get("histogram", True))
    periods = data.get("periods")  # optional: [{"year":..,"start_month":..,"end_month":..,"label":..}, ...]
    satellite = resolve_satellite(data.get("satellite"))
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))

    aoi = create_geometry_from_payload(data["aoi"])

    valid_indices = [idx for idx in indices if idx in VEGETATION_INDICES]
    skipped_indices = [
        {"index": idx, "reason": "Indeks tidak dikenal dalam katalog"}
        for idx in indices if idx not in VEGETATION_INDICES
    ]

    # ── Time series mode ──────────────────────────────────────────
    if periods:
        time_series: dict = {idx: [] for idx in valid_indices}
        period_labels = []
        for period in periods:
            p_year = int(period.get("year", year))
            p_start = int(period.get("start_month", start_month))
            p_end = int(period.get("end_month", end_month))
            label = period.get("label") or f"{p_year}-{p_start:02d}_{p_end:02d}"
            period_labels.append(label)

            composite, _, s_date, e_date, size, _valid_pct = _composite_for_period(db, aoi, p_year, p_start, p_end, cloud_threshold, satellite, cloud_mask_technique)
            for idx in valid_indices:
                if composite is None:
                    time_series[idx].append({"label": label, "date_range": None, "mean": None, "min": None, "max": None, "std_dev": None})
                    continue
                if not available_bands_for_index(idx, composite.bandNames().getInfo()):
                    time_series[idx].append({"label": label, "date_range": {"start": s_date, "end": e_date}, "mean": None, "min": None, "max": None, "std_dev": None})
                    continue
                index_image = calculate_index(composite, idx)
                stats = _index_stats(index_image, idx, aoi, veg_scale)
                time_series[idx].append({"label": label, "date_range": {"start": s_date, "end": e_date}, **stats})

        time_series_summary = {}
        for idx in valid_indices:
            means = [p["mean"] for p in time_series[idx]]
            trend = _trend_and_anomalies(means)
            time_series_summary[idx] = {
                "trend": trend["direction"],
                "slope": trend["slope"],
                "anomaly_periods": [period_labels[i] for i in trend["anomaly_indices"]],
            }

        return {
            "mode": "time_series",
            "periods": period_labels,
            "cloud_threshold": cloud_threshold,
            "scale": veg_scale,
            "satellite": get_satellite_meta(db, satellite),
            "cloud_mask_technique": cloud_mask_technique,
            "time_series": time_series,
            "time_series_summary": time_series_summary,
            "skipped_indices": skipped_indices,
        }

    # ── Single-period mode (default) ──────────────────────────────
    median_composite, _s2_collection, start_date, end_date, size, valid_pixel_pct = _composite_for_period(
        db, aoi, year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique
    )
    if median_composite is None:
        _sat_meta = get_satellite_meta(db, satellite)
        # `extra=` on AnalysisError is never actually forwarded to the client -
        # every route's exception handler does `HTTPException(detail=str(e))`,
        # dropping it (verified: no route reads `.extra` anywhere in this
        # codebase) - fold the hint into the message itself instead of a field
        # nobody reads.
        raise AnalysisError(
            f"Tidak ada citra {_sat_meta['name']} untuk periode ini. {_no_imagery_suggestion(_sat_meta, year)}",
            404,
        )

    results = {
        "collection_size": size,
        "date_range": {"start": start_date, "end": end_date},
        "cloud_threshold": cloud_threshold,
        "scale": veg_scale,
        "data_quality": {"valid_pixel_pct": valid_pixel_pct, "images_used": size},
        "satellite": get_satellite_meta(db, satellite),
        "cloud_mask_technique": cloud_mask_technique,
        "indices": {},
    }

    rgb_tile = get_tile_url(median_composite, {"bands": ["B4", "B3", "B2"], "min": 0, "max": 0.3}, "RGB")
    if rgb_tile:
        results["rgb_tile_url"] = rgb_tile["tile_url"]

    available_bands = median_composite.bandNames().getInfo()

    for idx in valid_indices:
        if not available_bands_for_index(idx, available_bands):
            required = VEGETATION_INDICES[idx]["bands"]
            skipped_indices.append({"index": idx, "reason": f"Band yang dibutuhkan ({', '.join(required)}) tidak tersedia pada sumber citra ini"})
            continue

        index_image = calculate_index(median_composite, idx)
        stats = _index_stats(index_image, idx, aoi, veg_scale)
        vis_params = {"min": VEGETATION_INDICES[idx]["range"][0], "max": VEGETATION_INDICES[idx]["range"][1], "palette": VEGETATION_INDICES[idx]["palette"]}
        tile = get_tile_url(index_image, vis_params, idx)

        classification = None
        if want_classify:
            classification = summarize_vegetation_classes(index_image, idx, aoi, veg_scale)
            if classification:
                classification["tile_url"] = vegetation_classification_tile(index_image, idx, idx)

        histogram = None
        if want_histogram:
            histogram = compute_index_histogram(index_image, aoi, veg_scale, VEGETATION_INDICES[idx]["range"][0], VEGETATION_INDICES[idx]["range"][1])

        results["indices"][idx] = {
            "min": stats["min"], "mean": stats["mean"],
            "max": stats["max"], "std_dev": stats["std_dev"],
            "description": VEGETATION_INDICES[idx]["description"],
            "tile_url": tile["tile_url"] if tile else None,
            "classification": classification,
            "histogram": histogram,
            "narrative": generate_index_narrative(idx, stats, classification),
        }

    if skipped_indices:
        results["skipped_indices"] = skipped_indices

    return results


def analyze_vegetation_compare(db, data: dict) -> dict:
    """Two-index comparison: quadrant area breakdown (high/low x high/low) + an
    auto-generated Indonesian insight, e.g. 'vegetasi rapat tapi lahan kering'."""
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    index_a = data.get("index_a", "NDVI")
    index_b = data.get("index_b", "NDMI")
    if index_a not in VEGETATION_INDICES or index_b not in VEGETATION_INDICES:
        raise AnalysisError("index_a/index_b harus ada di katalog indeks vegetasi", 400)

    year = int(data.get("year", 2022))
    start_month = int(data.get("start_month", 6))
    end_month = int(data.get("end_month", 9))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    veg_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["veg_scale"]))
    satellite = resolve_satellite(data.get("satellite"))
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))

    aoi = create_geometry_from_payload(data["aoi"])
    composite, _, start_date, end_date, _size, _valid_pct = _composite_for_period(db, aoi, year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique)
    if composite is None:
        _sat_meta = get_satellite_meta(db, satellite)
        raise AnalysisError(
            f"Tidak ada citra {_sat_meta['name']} untuk periode ini. {_no_imagery_suggestion(_sat_meta, year)}",
            404,
        )

    available_bands = composite.bandNames().getInfo()
    for idx in (index_a, index_b):
        if not available_bands_for_index(idx, available_bands):
            raise AnalysisError(f"Band untuk {idx} tidak tersedia pada sumber citra ini", 422)

    image_a = calculate_index(composite, index_a)
    image_b = calculate_index(composite, index_b)

    meta_a, meta_b = VEGETATION_INDICES[index_a], VEGETATION_INDICES[index_b]
    thr_a, thr_b = meta_a["comparison_threshold"], meta_b["comparison_threshold"]
    high_a = (image_a.gte(thr_a) if meta_a["polarity"] == "positive" else image_a.lt(thr_a))
    high_b = (image_b.gte(thr_b) if meta_b["polarity"] == "positive" else image_b.lt(thr_b))

    # quadrant code: 0=low/low, 1=low_a/high_b, 2=high_a/low_b, 3=high_a/high_b
    quadrant_code = high_a.toInt().multiply(2).add(high_b.toInt()).rename("quadrant")
    area_image = ee.Image.pixelArea().divide(10000).rename("area_ha").updateMask(quadrant_code.mask())
    grouped = area_image.addBands(quadrant_code).reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="quadrant"),
        geometry=aoi, scale=veg_scale, maxPixels=config_service.get_analysis_defaults(db)["max_pixels"], bestEffort=True, tileScale=4,
    ).getInfo()

    code_to_key = {0: "low_low", 1: "low_high", 2: "high_low", 3: "high_high"}
    raw = {item.get("quadrant"): float(item.get("sum", 0) or 0) for item in (grouped or {}).get("groups", [])}
    total = sum(raw.values()) or 1.0
    quadrants = {
        key: {"area_ha": round(raw.get(code, 0.0), 2), "percentage": round(raw.get(code, 0.0) / total * 100, 1)}
        for code, key in code_to_key.items()
    }

    return {
        "index_a": index_a, "index_b": index_b,
        "threshold_a": thr_a, "threshold_b": thr_b,
        "date_range": {"start": start_date, "end": end_date},
        "satellite": get_satellite_meta(db, satellite),
        "cloud_mask_technique": cloud_mask_technique,
        "total_area_ha": round(total, 2),
        "quadrants": quadrants,
        "insight": generate_comparison_narrative(index_a, index_b, quadrants),
    }


def analyze_timeseries(db, data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    year = int(data.get("year", 2022))
    index_name = data.get("index", "NDVI")
    interval = data.get("interval", "monthly")
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    veg_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["veg_scale"]))
    satellite = resolve_satellite(data.get("satellite"))
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))
    aoi = create_geometry_from_payload(data["aoi"])

    months = list(range(1, 13)) if interval == "monthly" else []

    time_series = []
    for month in months:
        label = f"{year}-{month:02d}"
        composite, _, _, _, _size, _valid_pct = _composite_for_period(db, aoi, year, month, month, cloud_threshold, satellite, cloud_mask_technique)
        if composite is not None:
            stats = calculate_index(composite, index_name).reduceRegion(
                reducer=ee.Reducer.mean(), geometry=aoi, scale=veg_scale,
                maxPixels=config_service.get_analysis_defaults(db)["max_pixels"], bestEffort=True
            ).getInfo()
            time_series.append({"period": label, "value": stats.get(index_name)})

    return {
        "index": index_name, "year": year, "interval": interval, "scale": veg_scale,
        "satellite": get_satellite_meta(db, satellite),
        "cloud_mask_technique": cloud_mask_technique,
        "data": time_series,
    }


def analyze_vegetation_change_hotspots(db, data: dict) -> dict:
    """Vectorize significant vegetation-index change between two periods into
    ranked polygons - the vegetation-side counterpart to
    landcover_service.analyze_landcover_hotspots, needed for the Geo-AI
    Assistant's `find_hotspots(metric="vegetation_change")` tool (P0 hotspot
    search, spec section 4).

    Unlike the landcover version (categorical class-transition labels packed
    into an integer band), this is a continuous index difference: build the
    index for both periods, threshold the (to - from) delta, and vectorize
    the resulting binary mask directly via reduceToVectors - no label-packing
    trick needed since there's only one "changed" class here. Per-polygon
    mean index change is attached afterwards via reduceRegions, same pattern
    analyze_landcover_hotspots uses for its Dynamic World confidence band.
    """
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    index_name = data.get("index", "NDVI")
    if index_name not in VEGETATION_INDICES:
        raise AnalysisError(f"Unknown vegetation index: {index_name}", 400)

    from_year = data.get("from_year", data.get("start_year"))
    to_year = data.get("to_year", data.get("end_year"))
    if from_year is None or to_year is None:
        raise AnalysisError("from_year and to_year are required", 400)
    from_year, to_year = int(from_year), int(to_year)

    start_month = int(data.get("start_month", 6))
    end_month = int(data.get("end_month", 9))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    satellite = resolve_satellite(data.get("satellite"))
    cloud_mask_technique = resolve_cloud_mask_technique(data.get("cloud_mask_technique"))

    # decline = index dropped (vegetation loss, the common case); increase = index rose;
    # any = |change| beyond threshold either direction.
    direction = data.get("direction", "decline")
    default_threshold = 0.2 if direction != "decline" else -0.2
    threshold = float(data.get("threshold", default_threshold))

    veg_scale = int(data.get("scale", config_service.get_analysis_defaults(db)["veg_scale"]))
    # reduceToVectors is a full connected-component pass (not a simple reduceRegion) -
    # default to coarser-than-native scale unless the caller overrides it, same
    # tractability reasoning as analyze_landcover_hotspots's vector_scale default.
    vector_scale = int(data.get("vector_scale", veg_scale * 5))
    min_area_ha = max(float(data.get("min_area_ha", 1.0)), 0.0)
    top_n = min(max(int(data.get("top_n", 20)), 1), 100)

    aoi = create_geometry_from_payload(data["aoi"])

    from_composite, _, from_start, from_end, _from_size, _from_valid_pct = _composite_for_period(
        db, aoi, from_year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique
    )
    to_composite, _, to_start, to_end, _to_size, _to_valid_pct = _composite_for_period(
        db, aoi, to_year, start_month, end_month, cloud_threshold, satellite, cloud_mask_technique
    )
    if from_composite is None or to_composite is None:
        _sat_meta = get_satellite_meta(db, satellite)
        _failed_year = from_year if from_composite is None else to_year
        raise AnalysisError(
            f"Tidak ada citra {_sat_meta['name']} untuk salah satu periode. {_no_imagery_suggestion(_sat_meta, _failed_year)}",
            404,
        )

    from_bands = from_composite.bandNames().getInfo()
    to_bands = to_composite.bandNames().getInfo()
    if not available_bands_for_index(index_name, from_bands) or not available_bands_for_index(index_name, to_bands):
        raise AnalysisError(f"Band untuk {index_name} tidak tersedia pada sumber citra ini", 422)

    from_index = calculate_index(from_composite, index_name)
    to_index = calculate_index(to_composite, index_name)
    change = to_index.subtract(from_index).rename("change")

    if direction == "decline":
        changed_mask = change.lte(threshold)
    elif direction == "increase":
        changed_mask = change.gte(threshold)
    else:
        changed_mask = change.abs().gte(abs(threshold))

    changed = changed_mask.selfMask().rename("changed")

    vectors = changed.reduceToVectors(
        geometry=aoi,
        scale=vector_scale,
        geometryType="polygon",
        eightConnected=True,
        reducer=ee.Reducer.countEvery(),
        maxPixels=int(get_settings().max_pixels),
        bestEffort=True,
        tileScale=4,
    )
    vectors = vectors.map(lambda f: f.set("area_ha", f.geometry().area(1).divide(10000)))
    vectors = vectors.filter(ee.Filter.gte("area_ha", min_area_ha)).sort("area_ha", False).limit(top_n)
    vectors = change.reduceRegions(collection=vectors, reducer=ee.Reducer.mean(), scale=veg_scale, tileScale=4)

    try:
        raw_features = vectors.getInfo().get("features", [])
    except Exception as e:  # noqa: BLE001
        logger.error(f"Vegetation hotspot vectorization failed: {e}")
        raise AnalysisError(
            "Vectorisasi hotspot vegetasi gagal (kemungkinan AOI terlalu besar untuk skala ini) - coba perbesar `scale`/`vector_scale` atau perkecil AOI.",
            422,
        )

    hotspots = []
    for f in raw_features:
        props = f.get("properties", {})
        geom = f.get("geometry")
        if not geom:
            continue
        centroid = None
        try:
            ring = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
            centroid = [round(sum(c[0] for c in ring) / len(ring), 6), round(sum(c[1] for c in ring) / len(ring), 6)]
        except Exception:  # noqa: BLE001, S110 - centroid is a display nicety; hotspot is still reported without it
            pass
        mean_change = props.get("mean")
        hotspots.append({
            "area_ha": round(float(props.get("area_ha", 0)), 2),
            "index": index_name,
            "index_change": round(float(mean_change), 4) if mean_change is not None else None,
            "index_change_percent": round(float(mean_change) * 100, 1) if mean_change is not None else None,
            "centroid": centroid,
            "geometry": geom,
        })

    return {
        "index": index_name,
        "direction": direction,
        "threshold": threshold,
        "from_year": from_year,
        "to_year": to_year,
        "date_range_from": {"start": from_start, "end": from_end},
        "date_range_to": {"start": to_start, "end": to_end},
        "satellite": get_satellite_meta(db, satellite),
        "cloud_mask_technique": cloud_mask_technique,
        "resolution": f"{veg_scale}m",
        "vector_scale": vector_scale,
        "min_area_ha": min_area_ha,
        "hotspot_count": len(hotspots),
        "hotspots": hotspots,
    }
