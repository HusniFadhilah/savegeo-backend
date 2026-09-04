"""ArcGIS-specific helpers shared by carbon and landcover services, plus the
`/api/arcgis/tiles/...` proxy route.

Ported verbatim (business logic unchanged) from legacy `backend/app.py` lines
~629-843 and ~1434-1623.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime

import requests

from app.providers.arcgis_client import _is_allowed_domain, get_arcgis_client
from app.registries.landcover_dataset_registry import LAND_COVER_LEGENDS

logger = logging.getLogger(__name__)

# In-memory cache: short hash -> Esri polygon dict (rings + spatialReference).
# Keyed by MD5 of the serialised polygon so identical AOIs share one entry.
# Ephemeral - cleared on process restart; tiles fall back to bbox-only clip on miss.
_CLIP_POLY_CACHE: dict = {}


def _store_clip_poly(esri_polygon: dict) -> str:
    key = hashlib.md5(json.dumps(esri_polygon, sort_keys=True).encode()).hexdigest()[:12]
    _CLIP_POLY_CACHE[key] = esri_polygon
    return key


def _get_clip_poly(key: str):
    return _CLIP_POLY_CACHE.get(key)


def _aoi_to_esri_polygon(aoi_payload: dict) -> dict:
    """Convert geomoka AOI payload to Esri JSON polygon for ArcGIS requests."""
    if "geojson" in aoi_payload:
        gj = aoi_payload["geojson"]
        if gj["type"] == "Feature":
            gj = gj["geometry"]
        elif gj["type"] == "FeatureCollection":
            rings = []
            for feat in gj.get("features", []):
                geom = feat.get("geometry") or {}
                if geom.get("type") == "Polygon":
                    rings.extend(geom.get("coordinates", []))
                elif geom.get("type") == "MultiPolygon":
                    rings.extend(ring for poly in geom.get("coordinates", []) for ring in poly)
            if rings:
                return {"rings": rings, "spatialReference": {"wkid": 4326}}
            raise ValueError("FeatureCollection has no polygon geometry")
        geom_type = gj.get("type")
        if geom_type == "Polygon":
            return {"rings": gj["coordinates"], "spatialReference": {"wkid": 4326}}
        if geom_type == "MultiPolygon":
            rings = [ring for poly in gj["coordinates"] for ring in poly]
            return {"rings": rings, "spatialReference": {"wkid": 4326}}
        flat = []

        def _collect(x):
            if x and isinstance(x[0], list):
                [_collect(i) for i in x]
            else:
                flat.append(x)

        _collect(gj.get("coordinates", []))
        lons = [p[0] for p in flat]
        lats = [p[1] for p in flat]
        w, e, s, n = min(lons), max(lons), min(lats), max(lats)
    else:
        w = float(aoi_payload["west"])
        e = float(aoi_payload["east"])
        s = float(aoi_payload["south"])
        n = float(aoi_payload["north"])
    return {
        "rings": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        "spatialReference": {"wkid": 4326},
    }


def _aoi_bbox(aoi_payload: dict):
    """Return (west, south, east, north) bbox from AOI payload."""
    if "geojson" in aoi_payload:
        gj = aoi_payload["geojson"]
        if gj["type"] == "Feature":
            gj = gj["geometry"]
        elif gj["type"] == "FeatureCollection":
            gj = {
                "type": "GeometryCollection",
                "geometries": [
                    feat.get("geometry") for feat in gj.get("features", [])
                    if feat.get("geometry")
                ],
            }
        flat = []

        def _collect(x):
            if not x:
                return
            if isinstance(x, dict):
                _collect(x.get("coordinates", []))
                for geom in x.get("geometries", []):
                    _collect(geom)
            elif x and isinstance(x[0], list):
                [_collect(i) for i in x]
            elif len(x) >= 2:
                flat.append(x)

        _collect(gj)
        if not flat:
            raise ValueError("AOI GeoJSON has no coordinates")
        lons = [p[0] for p in flat]
        lats = [p[1] for p in flat]
        return min(lons), min(lats), max(lons), max(lats)
    return (
        float(aoi_payload["west"]), float(aoi_payload["south"]),
        float(aoi_payload["east"]), float(aoi_payload["north"]),
    )


def _year_ms_range(year: int) -> str:
    """Return ArcGIS time parameter string for a full calendar year (UTC ms)."""
    start = int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    return f"{start},{end}"


def compute_arcgis_landcover_summary(dataset_key: str, ds_meta: dict, aoi_payload: dict, year: int) -> dict:
    """
    Get land cover class area statistics from ArcGIS ImageServer computeHistograms.
    Returns same dict format as GEE analyze_landcover results.
    """
    client = get_arcgis_client()
    if not client.is_enabled():
        raise RuntimeError("ArcGIS integration is disabled. Set ARCGIS_ENABLED=true in .env.")

    service_url = ds_meta.get("arcgis_service_url", "").rstrip("/")
    if not service_url or not _is_allowed_domain(service_url):
        raise RuntimeError(f"ArcGIS service URL not allowed: {service_url}")

    year_min = int(ds_meta.get("year_min", year))
    year_max = int(ds_meta.get("year_max", year))
    year_clamped = max(year_min, min(year_max, int(year)))

    # pixel area from resolution string (e.g. "300m" -> 300)
    resolution_str = ds_meta.get("resolution", "10m")
    resolution_m = int("".join(c for c in resolution_str if c.isdigit()) or "10")

    # For high-res datasets (< 300m), downsample to 300m for computeHistograms
    # to avoid ArcGIS "image exceeds size limit" error on large AOIs.
    # Area fractions remain accurate; only native-pixel count changes.
    STAT_RESOLUTION_CAP_M = 300
    stat_resolution_m = max(resolution_m, STAT_RESOLUTION_CAP_M)
    pixel_ha = (stat_resolution_m ** 2) / 10_000.0

    legend = LAND_COVER_LEGENDS.get(dataset_key, {})
    esri_polygon = _aoi_to_esri_polygon(aoi_payload)

    # pixelSize in decimal degrees (~300m at equator ~ 0.0027 deg)
    stat_deg = stat_resolution_m / 111_320.0

    params = {
        "geometry": json.dumps(esri_polygon),
        "geometryType": "esriGeometryPolygon",
        "spatialReference": json.dumps({"wkid": 4326}),
        "renderingRule": json.dumps({"rasterFunction": "None"}),
        "pixelSize": json.dumps({"x": stat_deg, "y": stat_deg, "spatialReference": {"wkid": 4326}}),
        "f": "json",
    }
    if ds_meta.get("arcgis_year_field"):
        year_field = ds_meta["arcgis_year_field"]
        params["mosaicRule"] = json.dumps({
            "mosaicMethod": "esriMosaicAttribute",
            "sortField": year_field,
            "sortValue": str(year_clamped),
            "ascending": True,
            "where": f"{year_field} = {int(year_clamped)}",
        })
    # only add time filter for time-aware services
    elif ds_meta.get("time_aware", True):
        params["time"] = _year_ms_range(year_clamped)
    if ds_meta.get("requires_auth", False):
        params.update(client._get_auth_params())

    resp = requests.post(f"{service_url}/computeHistograms", data=params, timeout=client._timeout)
    resp.raise_for_status()
    hist_data = resp.json()

    if "error" in hist_data:
        err = hist_data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise RuntimeError(f"ArcGIS ImageServer: {msg}")

    histograms = hist_data.get("histograms", [])
    if not histograms:
        raise RuntimeError("ArcGIS returned empty histogram - check time range or AOI coverage")

    h0 = histograms[0]
    counts = h0.get("counts", [])
    h_min = h0.get("min", 0)
    h_max = h0.get("max", len(counts) - 1)
    h_size = h0.get("size", len(counts)) or len(counts)
    bin_width = (h_max - h_min) / h_size if h_size > 0 else 1.0

    classes = {}
    for idx, count in enumerate(counts):
        class_value = round(h_min + bin_width * (idx + 0.5))
        key = str(class_value)
        if key in legend and count > 0:
            meta = legend[key]
            classes[meta["label"]] = {
                "area": round(count * pixel_ha, 2),
                "color": meta["color"],
                "pixel_count": count,
            }

    if not classes:
        raise RuntimeError("No land cover classes found in AOI - area may be outside dataset coverage")

    total_ha = sum(c["area"] for c in classes.values())
    for info in classes.values():
        info["percentage"] = round(info["area"] / total_ha * 100, 1) if total_ha else 0.0

    _w, _s, _e, _n = _aoi_bbox(aoi_payload)
    _clip_key = _store_clip_poly(esri_polygon)  # esri_polygon already computed above
    tile_url = (
        f"/api/arcgis/tiles/{dataset_key}/{{z}}/{{y}}/{{x}}"
        f"?year={year_clamped}"
        f"&clip_west={_w:.6f}&clip_south={_s:.6f}&clip_east={_e:.6f}&clip_north={_n:.6f}"
        f"&clip_key={_clip_key}"
    )

    return {
        "classes": classes,
        "tile_url": tile_url,
        "total_area_ha": round(total_ha, 2),
        "resolution": ds_meta.get("resolution", f"{resolution_m}m"),
        "year": year_clamped,
        "requested_year": year,
        "dataset_name": ds_meta.get("name", dataset_key),
        "date_range": {"start": f"{year_clamped}-01-01", "end": f"{year_clamped}-12-31"},
        "provider_type": ds_meta.get("provider_type"),
        "arcgis_item_id": ds_meta.get("arcgis_item_id"),
    }


def _arcgis_carbon_reference_stats(arcgis_meta: dict, aoi_geojson: dict) -> dict:
    """
    Compute carbon density statistics from an ArcGIS ImageServer for the given AOI.
    Returns dict with mean, std, min, max, p2, p98 (Mg C/ha), and vis_params.

    Uses computeHistograms endpoint. Some services only support envelope geometry -
    controlled by arcgis_meta["histogram_geom_type"].
    """
    service_url = arcgis_meta["arcgis_service_url"].rstrip("/")
    geom_type = arcgis_meta.get("histogram_geom_type", "esriGeometryPolygon")

    # Build geometry payload
    if geom_type == "esriGeometryEnvelope":
        # Extract bbox from GeoJSON polygon
        coords = []
        geojson_type = aoi_geojson.get("type", "")
        if geojson_type == "Polygon":
            for ring in aoi_geojson.get("coordinates", []):
                coords.extend(ring)
        elif geojson_type == "FeatureCollection":
            for feat in aoi_geojson.get("features", []):
                geom = feat.get("geometry", {})
                for ring in geom.get("coordinates", [[]]):
                    coords.extend(ring)
        elif geojson_type in ("MultiPolygon",):
            for poly in aoi_geojson.get("coordinates", []):
                for ring in poly:
                    coords.extend(ring)
        if coords:
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            geom_payload = {"xmin": min(lons), "ymin": min(lats), "xmax": max(lons), "ymax": max(lats)}
        else:
            geom_payload = {"xmin": 90, "ymin": -10, "xmax": 141, "ymax": 20}
    else:
        # Use polygon rings directly
        geojson_type = aoi_geojson.get("type", "")
        if geojson_type == "Polygon":
            rings = aoi_geojson.get("coordinates", [])
        elif geojson_type == "FeatureCollection":
            rings = []
            for feat in aoi_geojson.get("features", []):
                rings.extend(feat.get("geometry", {}).get("coordinates", []))
        else:
            rings = aoi_geojson.get("coordinates", [[]])
        geom_payload = {"rings": rings}

    resp = requests.post(
        f"{service_url}/computeHistograms",
        data={
            "geometry": json.dumps(geom_payload),
            "geometryType": geom_type,
            "spatialReference": json.dumps({"wkid": 4326}),
            "f": "json",
        },
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"ArcGIS computeHistograms error: {data['error']}")

    histograms = data.get("histograms", [])
    if not histograms:
        raise RuntimeError("ArcGIS returned empty histograms - no data in AOI")

    h = histograms[0]
    counts = h.get("counts", [])
    h_min = h.get("min", 0.0)
    h_max = h.get("max", 0.0)
    h_size = h.get("size", len(counts)) or len(counts)
    bin_width = (h_max - h_min) / h_size if h_size > 0 else 1.0

    total = sum(counts)
    if total == 0:
        raise RuntimeError("ArcGIS histogram has zero pixels in AOI")

    # Weighted mean and variance from histogram bins
    weighted_sum = 0.0
    weighted_sq_sum = 0.0
    running = 0
    p2_val = h_min
    p98_val = h_max
    p2_target = total * 0.02
    p98_target = total * 0.98

    for idx, count in enumerate(counts):
        center = h_min + bin_width * (idx + 0.5)
        weighted_sum += center * count
        weighted_sq_sum += (center ** 2) * count
        running += count
        if running <= p2_target:
            p2_val = center
        if running <= p98_target:
            p98_val = center

    mean_val = weighted_sum / total
    variance = (weighted_sq_sum / total) - (mean_val ** 2)
    std_val = math.sqrt(max(variance, 0.0))
    min_val = h_min
    max_val = h_max

    # Clip min/max using percentiles for vis
    vis_min_auto = max(0.0, round(p2_val, 1))
    vis_max_auto = round(p98_val, 1) or arcgis_meta.get("vis_max", 300)

    return {
        "mean": round(mean_val, 2),
        "std_dev": round(std_val, 2),
        "min": round(min_val, 2),
        "max": round(max_val, 2),
        "p2": round(p2_val, 2),
        "p98": round(p98_val, 2),
        "n_pixels": total,
        "vis_params": {
            "min": vis_min_auto,
            "max": vis_max_auto,
            "palette": arcgis_meta.get("vis_palette", ["f7fcf5", "74c476", "00441b"]),
        },
    }


def _is_biomass_carbon_vis_compatible(dataset_info: dict, vis_params: dict) -> bool:
    """Return True when reference vis params are safe for estimated biomass carbon."""
    if not isinstance(vis_params, dict):
        return False

    try:
        vis_min = float(vis_params.get("min"))
        vis_max = float(vis_params.get("max"))
    except (TypeError, ValueError):
        return False

    if vis_max <= vis_min:
        return False

    palette = vis_params.get("palette")
    if not isinstance(palette, list) or not palette:
        return False

    unit = str(dataset_info.get("unit") or "").lower()
    target_pool = str(dataset_info.get("target_pool") or "").lower()
    incompatible_terms = ("soil", "soc", "organic_carbon", "total_ecosystem")

    if any(term in target_pool for term in incompatible_terms):
        return False

    # Estimated carbon model output is density-like Mg/ha. Avoid driving its
    # display with SOC concentration/depth units.
    return "mg" in unit and "/ha" in unit


def _estimated_carbon_vis_params(
    default_vis: dict, reference_vis: dict, dataset_info: dict, match_reference: bool = True
) -> dict:
    if not match_reference:
        return dict(default_vis)
    if _is_biomass_carbon_vis_compatible(dataset_info, reference_vis):
        return {
            "min": reference_vis.get("min", default_vis.get("min")),
            "max": reference_vis.get("max", default_vis.get("max")),
            "palette": reference_vis.get("palette", default_vis.get("palette")),
        }
    return dict(default_vis)


def _gee_visualize_params(vis_params: dict) -> dict:
    """Strip UI-only metadata before passing params to ee.Image.visualize()."""
    if not isinstance(vis_params, dict):
        return {}
    allowed = {
        "bands",
        "min",
        "max",
        "gain",
        "bias",
        "gamma",
        "palette",
        "opacity",
        "forceRgbOutput",
    }
    return {key: value for key, value in vis_params.items() if key in allowed}
