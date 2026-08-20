"""GEE algebra / geometry helpers shared by carbon, vegetation, landcover, disaster,
and download services.

Ported verbatim (business logic unchanged) from legacy `backend/app.py` lines
~545-627 and ~844-920. Pure functions only - no FastAPI/Flask imports here.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import ee

from app.core.config import get_settings
from app.registries.vegetation_index_registry import VEGETATION_INDEX_CATALOG
from app.registries.satellite_provider_registry import CANONICAL_ALIAS


class AnalysisError(Exception):
    """Raised by service-layer analysis functions to signal a specific HTTP
    status code (400/404/422/503/413/...), mirroring the varied status codes
    the legacy Flask routes returned via `jsonify({"error": ...}), <code>`.
    Route handlers catch this and re-raise as `HTTPException(status_code, detail)`.
    """

    def __init__(self, message: str, status_code: int = 400, extra: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.extra = extra or {}


def mask_s2_clouds(image):
    qa = image.select("QA60")
    cloud = qa.bitwiseAnd(1 << 10).neq(0)
    cirrus = qa.bitwiseAnd(1 << 11).neq(0)
    return image.updateMask(cloud.Or(cirrus).Not()).divide(10000)


def mask_landsat_clouds(image):
    """Cloud/shadow mask + optical scale factor for Landsat Collection 2
    Level-2 surface reflectance (QA_PIXEL bits 3=cloud, 4=cloud shadow).
    Mirrors mask_s2_clouds's role for the Sentinel-2 path."""
    qa = image.select("QA_PIXEL")
    cloud = qa.bitwiseAnd(1 << 3).neq(0)
    shadow = qa.bitwiseAnd(1 << 4).neq(0)
    mask = cloud.Or(shadow).Not()
    optical = image.select("SR_B.*").multiply(0.0000275).add(-0.2)
    return image.addBands(optical, overwrite=True).updateMask(mask)


def standardize_bands(image, role_map: dict):
    """Rename a composite's sensor-native bands to the canonical
    Sentinel-2-style aliases (B2/B3/B4/B8/B11/...) so calculate_index() and
    available_bands_for_index() work identically regardless of which
    satellite produced the composite. Roles the sensor doesn't have (e.g.
    Landsat has no red-edge) are simply left out of the renamed image.

    `role_map` (role -> native band id, e.g. {"red": "SR_B4"}) comes from the
    caller already resolved with any DB override applied - see
    satellite_provider_repo.get_satellite_meta(). This function stays a pure
    ee-only helper with no DB/registry dependency of its own."""
    present = image.bandNames().getInfo()
    src_names, dst_names = [], []
    for role, native_band in role_map.items():
        if native_band in present:
            src_names.append(native_band)
            dst_names.append(CANONICAL_ALIAS[role])
    return image.select(src_names, dst_names)


def _calculate_evi(image):
    nir, red, blue = image.select("B8"), image.select("B4"), image.select("B2")
    return nir.subtract(red).multiply(2.5).divide(
        nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1)
    ).rename("EVI")


def calculate_index(image, index_name):
    if index_name == "EVI":
        return _calculate_evi(image)
    if index_name == "SAVI":
        nir, red = image.select("B8"), image.select("B4")
        return nir.subtract(red).divide(nir.add(red).add(0.5)).multiply(1.5).rename("SAVI")
    if index_name == "MSAVI":
        nir, red = image.select("B8"), image.select("B4")
        term = nir.multiply(2).add(1)
        return term.subtract(
            term.pow(2).subtract(nir.subtract(red).multiply(8)).sqrt()
        ).divide(2).rename("MSAVI")
    if index_name == "BSI":
        blue, red, nir, swir = image.select("B2"), image.select("B4"), image.select("B8"), image.select("B11")
        return swir.add(red).subtract(nir).subtract(blue).divide(
            swir.add(red).add(nir).add(blue)
        ).rename("BSI")
    if index_name == "ARVI":
        nir, red, blue = image.select("B8"), image.select("B4"), image.select("B2")
        rb = red.multiply(2).subtract(blue)
        return nir.subtract(rb).divide(nir.add(rb)).rename("ARVI")
    if index_name == "VARI":
        green, red, blue = image.select("B3"), image.select("B4"), image.select("B2")
        return green.subtract(red).divide(green.add(red).subtract(blue)).rename("VARI")
    if index_name == "SIPI":
        nir, blue, red = image.select("B8"), image.select("B2"), image.select("B4")
        return nir.subtract(blue).divide(nir.subtract(red)).rename("SIPI")
    if index_name == "GCI":
        nir, green = image.select("B8"), image.select("B3")
        return nir.divide(green).subtract(1).rename("GCI")
    if index_name == "LAI_PROXY":
        evi = _calculate_evi(image)
        return evi.multiply(3.618).subtract(0.118).max(0).rename("LAI_PROXY")
    return image.normalizedDifference(VEGETATION_INDEX_CATALOG[index_name]["bands"]).rename(index_name)


def create_geometry_from_payload(aoi_payload: dict) -> ee.Geometry:
    if not isinstance(aoi_payload, dict):
        raise ValueError("AOI payload must be an object")
    if "geojson" in aoi_payload:
        return geojson_to_ee_geometry(aoi_payload["geojson"])
    required = {"west", "south", "east", "north"}
    if not required.issubset(aoi_payload.keys()):
        raise ValueError("AOI bounds missing west/south/east/north")
    return ee.Geometry.Rectangle([
        float(aoi_payload["west"]), float(aoi_payload["south"]),
        float(aoi_payload["east"]), float(aoi_payload["north"]),
    ])


def geojson_to_ee_geometry(geojson):
    if geojson["type"] == "FeatureCollection":
        features = [ee.Feature(ee.Geometry(f["geometry"])) for f in geojson["features"]]
        return ee.FeatureCollection(features).geometry().dissolve()
    if geojson["type"] == "Feature":
        return ee.Geometry(geojson["geometry"])
    return ee.Geometry(geojson)


def geometry_area_ha(geometry) -> float:
    """Return geodesic geometry area in hectares."""
    return float(geometry.area(maxError=1).divide(10000).getInfo())


def build_date_range(year: int, start_month: int, end_month: int):
    start_date = f"{year}-{start_month:02d}-01"
    end_date = f"{year}-12-31" if end_month == 12 else f"{year}-{end_month + 1:02d}-01"
    return start_date, end_date


def get_tile_url(image, vis_params, name):
    import logging

    logger = logging.getLogger(__name__)
    try:
        map_id = image.getMapId(vis_params)
        return {"tile_url": map_id["tile_fetcher"].url_format, "name": name}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Tile URL error for {name}: {e}")
        return None


def _mask_s2_sr_clouds(image):
    scl = image.select("SCL")
    mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return image.updateMask(mask).divide(10000)


def _event_area_ha(mask_image, aoi, scale) -> float:
    settings = get_settings()
    area_img = ee.Image.pixelArea().divide(10000).updateMask(mask_image)
    stats = area_img.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=aoi,
        scale=scale,
        maxPixels=int(settings.max_pixels),
        bestEffort=True,
        tileScale=4,
    ).getInfo()
    return round(float(stats.get("area", 0) or 0), 2)


def _date_or_default(value: Optional[str], default_date: date) -> str:
    if not value:
        return default_date.strftime("%Y-%m-%d")
    datetime.strptime(value, "%Y-%m-%d")
    return value
