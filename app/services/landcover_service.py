"""Land cover analysis service.

Ported (business logic unchanged) from legacy `backend/app.py`:
  - get_landcover_image / summarize_landcover_classes / landcover_visual_image /
    summarize_landcover_image: lines ~921-1253
  - analyze_landcover route body: lines ~3626-3795
  - analyze_landcover_transition route body: lines ~3796-3953
  - analyze_landcover_change_map route body: lines ~3953-4053

`clamp_landcover_year` lived directly in legacy app.py (not the registry module),
so it is reproduced here rather than imported from
`app.registries.landcover_dataset_registry`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import ee

from app.core.config import get_settings
from app.registries.landcover_dataset_registry import (
    LAND_COVER_DATASET_OPTIONS,
    LAND_COVER_LEGENDS,
    LAND_COVER_NATIVE_SCALE,
    LAND_COVER_VIS,
    _landcover_year_bound,
    glad_glcluc_geotiff_uri,
    mapbiomas_indonesia_geotiff_uri,
    should_skip_lulc_class,
    supported_glc_fcs30d_year,
)
from app.services.arcgis_helpers import compute_arcgis_landcover_summary
from app.services.gee_common import (
    AnalysisError,
    build_date_range,
    create_geometry_from_payload,
    get_tile_url,
)

logger = logging.getLogger(__name__)

# Legacy defaults (env-var backed in Flask; no DB-backed system_config override
# ported yet, so these mirror `os.getenv("DEFAULT_YEAR_MIN", 2015)` /
# `os.getenv("DEFAULT_YEAR_MAX", datetime.now().year)`).
_DEFAULT_YEAR_MIN = 2015
_ESA_THRESHOLD_YEAR = 2021
_ESRI_COLLECTION_ID = LAND_COVER_DATASET_OPTIONS.get("ESRI_LandCover", {}).get(
    "gee_id", "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS"
)


def _default_year_max() -> int:
    return datetime.now().year


def clamp_landcover_year(dataset: str, year: int) -> int:
    if dataset == "GLC_FCS30D":
        return supported_glc_fcs30d_year(year)

    meta = LAND_COVER_DATASET_OPTIONS.get(dataset, {})
    requested = int(year)
    min_year = int(meta.get("year_min", _DEFAULT_YEAR_MIN))
    max_year = _landcover_year_bound(meta.get("year_max", _default_year_max()))
    return max(min_year, min(max_year, requested))


# ─────────────────────────────────────────────
# Core GEE helpers (ported ~verbatim)
# ─────────────────────────────────────────────

def get_landcover_image(
    dataset: str,
    year: int,
    aoi,
    start_month: int = 1,
    end_month: int = 12,
    dw_probability_threshold: Optional[float] = None,
):
    """Return a single class-label image and effective metadata for a LULC dataset.

    Args:
        dw_probability_threshold: If set (0-1), mask Dynamic World pixels whose max
            probability across all 9 bands is below the threshold.
    """
    requested_year = int(year)
    effective_year = clamp_landcover_year(dataset, requested_year)
    start_date, end_date = build_date_range(effective_year, start_month, end_month)

    if dataset == "Dynamic_World":
        dw_prob_bands = ["water", "trees", "grass", "flooded_vegetation",
                          "crops", "shrub_and_scrub", "built", "bare", "snow_and_ice"]
        dw = (
            ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
            .filterDate(start_date, end_date)
            .filterBounds(aoi)
        )
        if dw.size().getInfo() == 0:
            raise ValueError(f"No Dynamic World data found for {effective_year} ({start_date}-{end_date})")
        label_mode = dw.select("label").reduce(ee.Reducer.mode()).rename("landcover")
        if dw_probability_threshold is not None and 0 < dw_probability_threshold < 1:
            max_prob = dw.select(dw_prob_bands).reduce(ee.Reducer.max()).reduce(ee.Reducer.max())
            confidence_mask = max_prob.gte(dw_probability_threshold)
            label_mode = label_mode.updateMask(confidence_mask)
        image = label_mode.clip(aoi)
        return image, {
            "dataset": dataset,
            "dataset_name": "Dynamic World",
            "requested_year": requested_year,
            "year": effective_year,
            "date_range": {"start": start_date, "end": end_date},
            "provider_type": "gee_near_real_time",
            "dw_probability_threshold": dw_probability_threshold,
        }

    if dataset == "ESA_WorldCover":
        esa_threshold = _ESA_THRESHOLD_YEAR
        esa_year = effective_year
        col_id = "ESA/WorldCover/v200" if esa_year >= esa_threshold else "ESA/WorldCover/v100"
        image = ee.ImageCollection(col_id).first().select("Map").clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": f"ESA WorldCover v{200 if esa_year >= esa_threshold else 100}",
            "requested_year": requested_year,
            "year": esa_year,
            "date_range": {"start": f"{esa_year}-01-01", "end": f"{esa_year}-12-31"},
        }

    if dataset == "ESRI_LandCover":
        esri_year = effective_year
        col_id = _ESRI_COLLECTION_ID
        esri_col = ee.ImageCollection(col_id).filterDate(f"{esri_year}-01-01", f"{esri_year}-12-31").filterBounds(aoi)
        if esri_col.size().getInfo() == 0:
            esri_col = ee.ImageCollection(col_id).filterDate(f"{esri_year}-01-01", f"{esri_year}-12-31")
        image = esri_col.mosaic().clip(aoi).rename("landcover")
        fallback_reason = None
        if requested_year != esri_year:
            fallback_reason = f"Latest annual Esri LULC available is {esri_year}; annual product for {requested_year} not yet released."
        return image, {
            "dataset": dataset,
            "dataset_name": "ESRI 10m Land Cover",
            "requested_year": requested_year,
            "year": esri_year,
            "date_range": {"start": f"{esri_year}-01-01", "end": f"{esri_year}-12-31"},
            "fallback_reason": fallback_reason,
        }

    if dataset == "MODIS_LandCover":
        modis_year = effective_year
        modis_col = ee.ImageCollection("MODIS/061/MCD12Q1").filterDate(f"{modis_year}-01-01", f"{modis_year}-12-31")
        if modis_col.size().getInfo() == 0:
            modis_col = ee.ImageCollection("MODIS/061/MCD12Q1").limit(1, "system:time_start", False)
        image = modis_col.first().select("LC_Type1").clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": "MODIS MCD12Q1 IGBP",
            "requested_year": requested_year,
            "year": modis_year,
            "date_range": {"start": f"{modis_year}-01-01", "end": f"{modis_year}-12-31"},
        }

    if dataset == "Copernicus_LandCover":
        cop_year = effective_year
        cop_col = ee.ImageCollection("COPERNICUS/Landcover/100m/Proba-V-C3/Global").filterDate(f"{cop_year}-01-01", f"{cop_year}-12-31")
        if cop_col.size().getInfo() == 0:
            cop_col = ee.ImageCollection("COPERNICUS/Landcover/100m/Proba-V-C3/Global").limit(1, "system:time_start", False)
        image = cop_col.first().select("discrete_classification").clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": "Copernicus Global Land Cover 100m",
            "requested_year": requested_year,
            "year": cop_year,
            "date_range": {"start": f"{cop_year}-01-01", "end": f"{cop_year}-12-31"},
        }

    if dataset == "GLC_FCS30D":
        if effective_year >= 2000:
            band_name = f"b{effective_year - 1999}"
            collection_id = "projects/sat-io/open-datasets/GLC-FCS30D/annual"
        else:
            band_name = {1985: "b1", 1990: "b2", 1995: "b3"}[effective_year]
            collection_id = "projects/sat-io/open-datasets/GLC-FCS30D/five-years-map"

        image = (
            ee.ImageCollection(collection_id)
            .select(band_name)
            .mosaic()
            .clip(aoi)
            .rename("landcover")
        )
        return image, {
            "dataset": dataset,
            "dataset_name": "GLC_FCS30D",
            "requested_year": requested_year,
            "year": effective_year,
            "date_range": {"start": f"{effective_year}-01-01", "end": f"{effective_year}-12-31"},
            "band": band_name,
            "collection_id": collection_id,
        }

    if dataset == "C3S_LandCover":
        c3s_year = effective_year
        c3s_col = ee.ImageCollection("projects/sat-io/open-datasets/ESA/C3S-LC-L4-LCCS").filterDate(
            f"{c3s_year}-01-01", f"{c3s_year}-12-31"
        )
        if c3s_col.size().getInfo() == 0:
            c3s_col = ee.ImageCollection("projects/sat-io/open-datasets/ESA/C3S-LC-L4-LCCS").filter(
                ee.Filter.calendarRange(c3s_year, c3s_year, "year")
            )
        if c3s_col.size().getInfo() == 0:
            c3s_col = ee.ImageCollection("projects/sat-io/open-datasets/ESA/C3S-LC-L4-LCCS").limit(1, "system:time_start", False)
        image = c3s_col.first().select("b1").clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": "C3S Land Cover",
            "requested_year": requested_year,
            "year": c3s_year,
            "date_range": {"start": f"{c3s_year}-01-01", "end": f"{c3s_year}-12-31"},
            "collection_id": "projects/sat-io/open-datasets/ESA/C3S-LC-L4-LCCS",
        }

    if dataset == "JAXA_FNF":
        fnf_year = effective_year
        fnf_col = ee.ImageCollection("JAXA/ALOS/PALSAR/YEARLY/FNF").filterDate(f"{fnf_year}-01-01", f"{fnf_year}-12-31")
        if fnf_col.size().getInfo() == 0:
            fnf_col = ee.ImageCollection("JAXA/ALOS/PALSAR/YEARLY/FNF").limit(1, "system:time_start", False)
        image = fnf_col.first().select("fnf").clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": "JAXA ALOS Forest/Non-Forest",
            "requested_year": requested_year,
            "year": fnf_year,
            "date_range": {"start": f"{fnf_year}-01-01", "end": f"{fnf_year}-12-31"},
        }

    if dataset == "JAXA_FNF4":
        fnf_year = effective_year
        fnf_col = ee.ImageCollection("JAXA/ALOS/PALSAR/YEARLY/FNF4").filterDate(f"{fnf_year}-01-01", f"{fnf_year}-12-31")
        if fnf_col.size().getInfo() == 0:
            fnf_col = ee.ImageCollection("JAXA/ALOS/PALSAR/YEARLY/FNF4").limit(1, "system:time_start", False)
        image = fnf_col.first().select("fnf").clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": "JAXA PALSAR Forest/Non-Forest 4-class",
            "requested_year": requested_year,
            "year": fnf_year,
            "date_range": {"start": f"{fnf_year}-01-01", "end": f"{fnf_year}-12-31"},
        }

    if dataset == "MapBiomas_Indonesia":
        uri, mbi_meta = mapbiomas_indonesia_geotiff_uri(requested_year)
        mbi_year = mbi_meta["year"]
        image = ee.Image.loadGeoTIFF(uri).clip(aoi).rename("landcover")
        return image, {
            "dataset": dataset,
            "dataset_name": f"MapBiomas Indonesia LANDY {mbi_meta['collection']}",
            "requested_year": requested_year,
            "year": mbi_year,
            "date_range": {"start": f"{mbi_year}-01-01", "end": f"{mbi_year}-12-31"},
            "source_uri": uri,
        }

    # ── New provider types ──────────────────────────────────────────────

    if dataset == "JRC_TMF":
        band_name = f"Dec{effective_year}"
        image = (
            ee.ImageCollection("JRC/TMF/v1_2022/AnnualChanges")
            .select(band_name)
            .mosaic()
            .clip(aoi)
            .rename("landcover")
        )
        return image, {
            "dataset": dataset,
            "dataset_name": "JRC Tropical Moist Forest Annual Changes",
            "requested_year": requested_year,
            "year": effective_year,
            "date_range": {"start": f"{effective_year}-01-01", "end": f"{effective_year}-12-31"},
            "provider_type": "gee_official",
        }

    if dataset == "FROM_GLC10":
        image = (
            ee.Image("Tsinghua/FROM-GLC10/2017V01")
            .select("b1")
            .clip(aoi)
            .rename("landcover")
        )
        return image, {
            "dataset": dataset,
            "dataset_name": "Tsinghua FROM-GLC 10m Land Cover 2017",
            "requested_year": requested_year,
            "year": effective_year,  # always 2017 via clamp_landcover_year
            "date_range": {"start": "2017-01-01", "end": "2017-12-31"},
            "provider_type": "gee_official",
        }

    if dataset == "GLAD_GLCLUC":
        uri, glcluc_meta = glad_glcluc_geotiff_uri(requested_year)
        glcluc_year = glcluc_meta["year"]
        try:
            image = ee.Image.loadGeoTIFF(uri).select(0).clip(aoi).rename("landcover")
        except Exception as load_err:
            raise ValueError(
                f"GLAD_GLCLUC: ee.Image.loadGeoTIFF failed for URI '{uri}'. "
                f"Verify GCS path is a public COG. Original error: {load_err}"
            )
        return image, {
            "dataset": dataset,
            "dataset_name": "GLAD Annual Global Land Use/Land Cover",
            "requested_year": requested_year,
            "year": glcluc_year,
            "date_range": {"start": f"{glcluc_year}-01-01", "end": f"{glcluc_year}-12-31"},
            "provider_type": "cloud_geotiff",
            "source_uri": uri,
        }

    raise ValueError(f"Unknown dataset: {dataset}")


def summarize_landcover_classes(dataset: str, image, aoi, lc_scale: int, include_improbable_classes: bool = False, band_name=None):
    settings = get_settings()
    lc_scale = max(int(lc_scale), LAND_COVER_NATIVE_SCALE.get(dataset, int(lc_scale)))
    class_band = image.select(band_name) if band_name else image.select(0)
    class_band = class_band.rename("class").toInt16()
    area_image = ee.Image.pixelArea().divide(10000).rename("area_ha").updateMask(class_band.mask())
    grouped = area_image.addBands(class_band).reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="class_value"),
        geometry=aoi,
        scale=lc_scale,
        maxPixels=int(settings.max_pixels),
        bestEffort=True,
        tileScale=4,
    ).getInfo()

    classes = {}
    valid_items = []
    for item in (grouped or {}).get("groups", []):
        class_val = item.get("class_value")
        area_ha = float(item.get("sum", 0) or 0)
        class_key = str(int(float(class_val)))
        if class_key in LAND_COVER_LEGENDS[dataset]:
            label = LAND_COVER_LEGENDS[dataset][class_key]["label"]
            if should_skip_lulc_class(label, include_improbable_classes):
                continue
            valid_items.append((class_key, label, area_ha))

    total_area_ha = sum(area_ha for _, _, area_ha in valid_items)
    if not total_area_ha:
        return None

    for class_key, label, area_ha in valid_items:
        classes[label] = {
            "area": round(area_ha, 2),
            "percentage": round(area_ha / total_area_ha * 100, 1),
            "color": LAND_COVER_LEGENDS[dataset][class_key]["color"],
            "pixel_count": int(round(area_ha * 10000 / (lc_scale ** 2))),
            "class_value": int(class_key),
        }

    return classes, round(total_area_ha, 2), lc_scale


def landcover_visual_image(dataset: str, image):
    legend = LAND_COVER_LEGENDS.get(dataset)
    if not legend:
        return image, LAND_COVER_VIS.get(dataset, {"min": 0, "max": 255})

    class_values = [int(value) for value in legend.keys()]
    palette = [legend[str(value)]["color"].replace("#", "") for value in class_values]
    visual_values = list(range(len(class_values)))
    visual = image.select(0).remap(class_values, visual_values).rename("landcover_visual")
    return visual, {"min": 0, "max": max(len(class_values) - 1, 0), "palette": palette}


def summarize_landcover_image(dataset: str, image, aoi, lc_scale: int, metadata: dict, include_improbable_classes: bool = False):
    summary = summarize_landcover_classes(dataset, image, aoi, lc_scale, include_improbable_classes)
    if not summary:
        return None

    classes, total_area_ha, lc_scale = summary

    visual_image, visual_params = landcover_visual_image(dataset, image)
    tile = get_tile_url(visual_image, visual_params, metadata["dataset_name"])
    return {
        "classes": classes,
        "tile_url": tile["tile_url"] if tile else None,
        "total_area_ha": total_area_ha,
        "resolution": f"{lc_scale}m",
        "requested_year": metadata.get("requested_year"),
        "year": metadata["year"],
        "dataset_name": metadata["dataset_name"],
        "date_range": metadata.get("date_range"),
    }


# ─────────────────────────────────────────────
# Route-body logic
# ─────────────────────────────────────────────

def analyze_landcover(data: dict) -> dict:
    if not data:
        raise AnalysisError("Body JSON kosong atau tidak valid", 400)
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    settings = get_settings()
    aoi = create_geometry_from_payload(data["aoi"])
    datasets = data.get("datasets", ["Dynamic_World"])
    dw_probability_threshold = data.get("dw_probability_threshold")
    if dw_probability_threshold is not None:
        dw_probability_threshold = float(dw_probability_threshold)
    lc_scale = int(data.get("scale", settings.default_landcover_scale))
    include_improbable_classes = bool(data.get("include_improbable_classes", False))
    results: dict = {}

    # Resolve date params - supports specific date range or year/month
    explicit_start = data.get("start_date")  # "YYYY-MM-DD"
    explicit_end = data.get("end_date")  # "YYYY-MM-DD"
    if explicit_start and explicit_end:
        from datetime import date as _date

        sd = _date.fromisoformat(explicit_start)
        year = sd.year
        start_month = sd.month
        end_month = _date.fromisoformat(explicit_end).month
        dw_start_date = explicit_start
        dw_end_date = explicit_end
        dw_year = year
    else:
        year = int(data.get("year", 2022))
        start_month = int(data.get("start_month", 1))
        end_month = int(data.get("end_month", 12))
        dw_year = clamp_landcover_year("Dynamic_World", year)
        dw_start_date, dw_end_date = build_date_range(dw_year, start_month, end_month)

    # Dynamic World (gee_near_real_time - supports current year and beyond)
    if "Dynamic_World" in datasets:
        dw_prob_bands = ["water", "trees", "grass", "flooded_vegetation", "crops", "shrub_and_scrub", "built", "bare", "snow_and_ice"]
        dw = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterDate(dw_start_date, dw_end_date).filterBounds(aoi)
        if dw.size().getInfo() > 0:
            classification = dw.select("label").reduce(ee.Reducer.mode()).clip(aoi)
            if dw_probability_threshold is not None and 0 < dw_probability_threshold < 1:
                max_prob = dw.select(dw_prob_bands).reduce(ee.Reducer.max()).reduce(ee.Reducer.max())
                classification = classification.updateMask(max_prob.gte(dw_probability_threshold))
            summary = summarize_landcover_classes(
                "Dynamic_World", classification, aoi, lc_scale,
                include_improbable_classes, band_name="label_mode"
            )
            if summary:
                classes, total_area_ha, effective_scale = summary
                vis = {"min": 0, "max": 8, "palette": ["#419BDF", "#397D49", "#88B053", "#7A87C6", "#E49635", "#DFC35A", "#C4281B", "#A59B8F", "#B39FE1"]}
                tile = get_tile_url(classification, vis, "Dynamic World")
                results["Dynamic_World"] = {
                    "classes": classes, "tile_url": tile["tile_url"] if tile else None,
                    "total_area_ha": total_area_ha,
                    "resolution": f"{effective_scale}m",
                    "dataset_name": "Dynamic World",
                    "requested_year": year,
                    "year": dw_year,
                    "date_range": {"start": dw_start_date, "end": dw_end_date},
                    "provider_type": "gee_near_real_time",
                    "dw_probability_threshold": dw_probability_threshold,
                }

    # ESA WorldCover
    if "ESA_WorldCover" in datasets:
        try:
            esa_threshold = _ESA_THRESHOLD_YEAR
            esa_year = clamp_landcover_year("ESA_WorldCover", year)
            col_id = "ESA/WorldCover/v200" if esa_year >= esa_threshold else "ESA/WorldCover/v100"
            esa_clipped = ee.ImageCollection(col_id).first().select("Map").clip(aoi)
            summary = summarize_landcover_classes(
                "ESA_WorldCover", esa_clipped, aoi, lc_scale,
                include_improbable_classes, band_name="Map"
            )
            if summary:
                classes, total_area_ha, effective_scale = summary
                vis = {"min": 10, "max": 100, "palette": ["006400", "ffbb22", "ffff4c", "f096ff", "fa0000", "b4b4b4", "f0f0f0", "0032c8", "0096a0", "00cf75", "fae6a0"]}
                rgb_map = esa_clipped.visualize(**vis).getMapId()
                results["ESA_WorldCover"] = {
                    "classes": classes, "tile_url": rgb_map["tile_fetcher"].url_format,
                    "total_area_ha": total_area_ha,
                    "resolution": f"{effective_scale}m", "year": esa_year,
                    "requested_year": year,
                    "dataset_name": f"ESA WorldCover v{200 if esa_year >= esa_threshold else 100}",
                    "date_range": {"start": f"{esa_year}-01-01", "end": f"{esa_year}-12-31"},
                }
        except Exception as e:  # noqa: BLE001
            logger.error(f"ESA WorldCover error: {e}")

    # ESRI Land Cover (annual; fallback for requests beyond latest)
    if "ESRI_LandCover" in datasets:
        try:
            esri_year = clamp_landcover_year("ESRI_LandCover", year)
            esri_fallback_reason = None
            if year != esri_year:
                esri_fallback_reason = (
                    f"Latest annual Esri LULC available is {esri_year}; "
                    f"annual product for {year} not yet released. "
                    "Use Dynamic_World for near-real-time current-year LULC."
                )
            col_id = _ESRI_COLLECTION_ID
            esri_col = ee.ImageCollection(col_id).filterDate(f"{esri_year}-01-01", f"{esri_year}-12-31").filterBounds(aoi)
            if esri_col.size().getInfo() == 0:
                esri_col = ee.ImageCollection(col_id).filterDate(f"{esri_year}-01-01", f"{esri_year}-12-31")
            esri_clipped = esri_col.mosaic().clip(aoi)
            band_name = esri_clipped.bandNames().getInfo()[0]
            summary = summarize_landcover_classes(
                "ESRI_LandCover", esri_clipped, aoi, lc_scale,
                include_improbable_classes, band_name=band_name
            )
            if summary:
                classes, total_area_ha, effective_scale = summary
                visual_image, visual_params = landcover_visual_image("ESRI_LandCover", esri_clipped.select(band_name))
                tile = get_tile_url(visual_image, visual_params, "ESRI 10m Annual LULC v3")
                results["ESRI_LandCover"] = {
                    "classes": classes, "tile_url": tile["tile_url"] if tile else None,
                    "total_area_ha": total_area_ha,
                    "resolution": f"{effective_scale}m", "year": esri_year,
                    "requested_year": year,
                    "dataset_name": "ESRI 10m Annual LULC v3",
                    "date_range": {"start": f"{esri_year}-01-01", "end": f"{esri_year}-12-31"},
                    "fallback_reason": esri_fallback_reason,
                }
        except Exception as e:  # noqa: BLE001
            logger.error(f"ESRI error: {e}")

    handled_datasets = {"Dynamic_World", "ESA_WorldCover", "ESRI_LandCover"}
    for dataset in datasets:
        if dataset not in handled_datasets:
            ds_meta = LAND_COVER_DATASET_OPTIONS.get(dataset, {})
            if ds_meta.get("provider_type", "").startswith("arcgis") and not ds_meta.get("gee_id"):
                try:
                    results[dataset] = compute_arcgis_landcover_summary(
                        dataset, ds_meta, data["aoi"], year
                    )
                except Exception as arc_err:  # noqa: BLE001
                    logger.error(f"ArcGIS {dataset} analysis error: {arc_err}")
                    results[dataset] = {
                        "error": str(arc_err),
                        "provider_type": ds_meta.get("provider_type"),
                        "arcgis_item_id": ds_meta.get("arcgis_item_id"),
                        "status": "arcgis_error",
                    }
                continue
            try:
                lc_image, metadata = get_landcover_image(dataset, year, aoi, start_month, end_month)
                summary = summarize_landcover_image(dataset, lc_image, aoi, lc_scale, metadata, include_improbable_classes)
                if summary:
                    results[dataset] = summary
            except Exception as e:  # noqa: BLE001
                logger.error(f"{dataset} error: {e}")
                results[dataset] = {"error": str(e)}

    success_count = sum(1 for v in results.values() if "error" not in v)
    if not results or success_count == 0 and not any("status" in v for v in results.values()):
        raise AnalysisError("Tidak ada data land cover yang berhasil diproses", 404)
    return results


def analyze_landcover_transition(data: dict) -> dict:
    if not data:
        raise AnalysisError("Body JSON kosong atau tidak valid", 400)
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    settings = get_settings()
    dataset = str(data.get("dataset", "Dynamic_World")).strip()
    if dataset not in LAND_COVER_LEGENDS:
        raise AnalysisError(f"Unknown dataset: {dataset}", 400)

    ds_opts = LAND_COVER_DATASET_OPTIONS.get(dataset, {})
    if not ds_opts.get("supports_transition", True):
        raise AnalysisError(
            f"Dataset '{dataset}' tidak mendukung analisis transisi.",
            400,
            extra={"dataset": dataset, "supports_transition": False},
        )

    start_year = int(data.get("start_year", data.get("year", datetime.now().year - 1)))
    end_year = int(data.get("end_year", data.get("year", datetime.now().year)))
    if start_year >= end_year:
        raise AnalysisError("start_year harus lebih kecil dari end_year", 400)

    start_month = int(data.get("start_month", 1))
    end_month = int(data.get("end_month", 12))
    lc_scale = int(data.get("scale", settings.default_landcover_scale))
    lc_scale = max(lc_scale, LAND_COVER_NATIVE_SCALE.get(dataset, lc_scale))
    include_improbable_classes = bool(data.get("include_improbable_classes", False))
    if not (1 <= start_month <= 12 and 1 <= end_month <= 12 and start_month <= end_month):
        raise AnalysisError("Rentang bulan tidak valid", 400)

    aoi = create_geometry_from_payload(data["aoi"])
    legend = LAND_COVER_LEGENDS[dataset]
    years = list(range(start_year, end_year + 1))
    images = {}
    metadata = {}

    for year in years:
        image, meta = get_landcover_image(dataset, year, aoi, start_month, end_month)
        images[year] = image
        metadata[year] = meta

    pairs = []
    class_totals: dict = {}
    for prev_year, next_year in zip(years[:-1], years[1:]):
        transition_image = images[prev_year].toInt16().multiply(1000).add(images[next_year].toInt16()).rename("transition")
        area_by_transition = (
            ee.Image.pixelArea().divide(10000).rename("area_ha")
            .addBands(transition_image)
        )
        grouped = area_by_transition.reduceRegion(
            reducer=ee.Reducer.sum().group(groupField=1, groupName="transition"),
            geometry=aoi,
            scale=lc_scale,
            maxPixels=int(settings.max_pixels),
            bestEffort=True,
            tileScale=4,
        ).getInfo()

        raw_groups = grouped.get("groups", []) if grouped else []
        flows = []
        total_area = 0
        changed_area = 0
        stable_area = 0

        for item in raw_groups:
            transition_key = item.get("transition")
            code = int(float(transition_key))
            from_class = str(code // 1000)
            to_class = str(code % 1000)
            if from_class not in legend or to_class not in legend:
                continue

            from_label = legend[from_class]["label"]
            to_label = legend[to_class]["label"]
            if (
                should_skip_lulc_class(from_label, include_improbable_classes)
                or should_skip_lulc_class(to_label, include_improbable_classes)
            ):
                continue

            area_ha = float(item.get("sum", 0) or 0)
            total_area += area_ha
            if from_class == to_class:
                stable_area += area_ha
            else:
                changed_area += area_ha

            class_totals.setdefault(from_label, {"loss": 0, "gain": 0, "color": legend[from_class]["color"]})
            class_totals.setdefault(to_label, {"loss": 0, "gain": 0, "color": legend[to_class]["color"]})
            if from_class != to_class:
                class_totals[from_label]["loss"] += area_ha
                class_totals[to_label]["gain"] += area_ha

            flows.append({
                "from": from_label,
                "to": to_label,
                "from_class": int(from_class),
                "to_class": int(to_class),
                "area": round(area_ha, 2),
                "percentage": 0,
                "pixel_count": int(round(area_ha * 10000 / (lc_scale ** 2))),
                "from_color": legend[from_class]["color"],
                "to_color": legend[to_class]["color"],
                "changed": from_class != to_class,
            })

        for flow in flows:
            flow["percentage"] = round((flow["area"] / total_area) * 100, 2) if total_area else 0
        flows.sort(key=lambda item: item["area"], reverse=True)

        pairs.append({
            "from_year": prev_year,
            "to_year": next_year,
            "from_effective_year": metadata[prev_year]["year"],
            "to_effective_year": metadata[next_year]["year"],
            "flows": flows,
            "total_area_ha": round(total_area, 2),
            "changed_area_ha": round(changed_area, 2),
            "stable_area_ha": round(stable_area, 2),
            "changed_percentage": round((changed_area / total_area) * 100, 2) if total_area else 0,
            "stable_percentage": round((stable_area / total_area) * 100, 2) if total_area else 0,
        })

    net_changes = [
        {
            "class": label,
            "gain": round(values["gain"], 2),
            "loss": round(values["loss"], 2),
            "net": round(values["gain"] - values["loss"], 2),
            "color": values["color"],
        }
        for label, values in class_totals.items()
    ]
    net_changes.sort(key=lambda item: abs(item["net"]), reverse=True)

    return {
        "dataset": dataset,
        "dataset_name": metadata[years[0]]["dataset_name"],
        "requested_years": years,
        "effective_years": {str(year): metadata[year]["year"] for year in years},
        "resolution": f"{lc_scale}m",
        "month_range": {"start_month": start_month, "end_month": end_month},
        "pairs": pairs,
        "net_changes": net_changes,
    }


def analyze_landcover_change_map(data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    settings = get_settings()
    dataset = data.get("dataset", "Dynamic_World")
    if dataset not in LAND_COVER_LEGENDS:
        raise AnalysisError(f"Unknown dataset: {dataset}", 400)

    ds_opts_cm = LAND_COVER_DATASET_OPTIONS.get(dataset, {})
    if not ds_opts_cm.get("supports_transition", True):
        raise AnalysisError(
            f"Dataset '{dataset}' tidak mendukung analisis perubahan peta.",
            400,
            extra={"dataset": dataset, "supports_transition": False},
        )

    from_year = int(data.get("from_year"))
    to_year = int(data.get("to_year"))
    start_month = int(data.get("start_month", 1))
    end_month = int(data.get("end_month", 12))
    lc_scale = max(
        int(data.get("scale", LAND_COVER_NATIVE_SCALE.get(dataset, settings.default_landcover_scale))),
        LAND_COVER_NATIVE_SCALE.get(dataset, int(data.get("scale", settings.default_landcover_scale))),
    )

    aoi = create_geometry_from_payload(data["aoi"])
    from_image, from_meta = get_landcover_image(dataset, from_year, aoi, start_month, end_month)
    to_image, to_meta = get_landcover_image(dataset, to_year, aoi, start_month, end_month)

    changed = from_image.neq(to_image).rename("changed").selfMask().clip(aoi)
    stable = from_image.eq(to_image).rename("stable").selfMask().clip(aoi)
    to_changed = to_image.updateMask(changed).clip(aoi)

    area_ha = ee.Image.pixelArea().divide(10000)
    changed_area = area_ha.updateMask(changed).reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=aoi,
        scale=lc_scale,
        maxPixels=int(settings.max_pixels),
        bestEffort=True,
        tileScale=4,
    ).getInfo()
    stable_area = area_ha.updateMask(stable).reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=aoi,
        scale=lc_scale,
        maxPixels=int(settings.max_pixels),
        bestEffort=True,
        tileScale=4,
    ).getInfo()

    change_tile = get_tile_url(
        changed,
        {"min": 1, "max": 1, "palette": ["ff1744"]},
        "Changed area",
    )
    destination_tile = get_tile_url(
        *landcover_visual_image(dataset, to_changed),
        "Changed pixels by destination class",
    )
    from_tile = get_tile_url(
        *landcover_visual_image(dataset, from_image),
        "Land cover from year",
    )
    to_tile = get_tile_url(
        *landcover_visual_image(dataset, to_image),
        "Land cover to year",
    )

    changed_ha = float(changed_area.get("area_ha", 0) or changed_area.get("area", 0) or changed_area.get("constant", 0) or 0)
    stable_ha = float(stable_area.get("area_ha", 0) or stable_area.get("area", 0) or stable_area.get("constant", 0) or 0)
    total_ha = changed_ha + stable_ha

    return {
        "dataset": dataset,
        "dataset_name": LAND_COVER_DATASET_OPTIONS.get(dataset, {}).get("name", dataset),
        "from_year": from_year,
        "to_year": to_year,
        "from_effective_year": from_meta.get("year"),
        "to_effective_year": to_meta.get("year"),
        "resolution": f"{lc_scale}m",
        "changed_tile_url": change_tile["tile_url"] if change_tile else None,
        "destination_tile_url": destination_tile["tile_url"] if destination_tile else None,
        "from_tile_url": from_tile["tile_url"] if from_tile else None,
        "to_tile_url": to_tile["tile_url"] if to_tile else None,
        "changed_area_ha": round(changed_ha, 2),
        "stable_area_ha": round(stable_ha, 2),
        "changed_percentage": round((changed_ha / total_ha) * 100, 2) if total_ha else 0,
        "stable_percentage": round((stable_ha / total_ha) * 100, 2) if total_ha else 0,
    }
