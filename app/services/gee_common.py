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


# ─────────────────────────────────────────────
# Selectable Sentinel-2 cloud-masking technique
#
# Before this, three near-duplicate SCL/QA60 maskers were scattered across
# carbon_inference.py/gee_common.py with no way to pick one - this is the
# single place all Sentinel-2 pipelines can go through if they want the
# technique to be a request parameter instead of hardcoded per-file.
# ─────────────────────────────────────────────
CLOUD_MASK_TECHNIQUES = ("scl", "qa60", "s2cloudless")
DEFAULT_CLOUD_MASK_TECHNIQUE = "scl"

CLOUD_MASK_TECHNIQUE_INFO = {
    "scl": {
        "label": "SCL (Scene Classification Layer)",
        "description": "Klasifikasi per-piksel bawaan Sentinel-2 L2A (shadow/cloud-medium/cloud-high/cirrus dibuang). Cepat, standar industri.",
    },
    "qa60": {
        "label": "QA60 Bitmask",
        "description": "Flag cloud/cirrus level-scene dari band QA60. Lebih kasar dari SCL (resolusi mask lebih rendah) tapi lebih ringan secara komputasi.",
    },
    "s2cloudless": {
        "label": "s2cloudless (probability-based)",
        "description": "Model probabilitas awan per-piksel (COPERNICUS/S2_CLOUD_PROBABILITY), umumnya lebih akurat di tepi awan/awan tipis dibanding SCL/QA60. Butuh join koleksi tambahan, sedikit lebih lambat.",
    },
}


def resolve_cloud_mask_technique(technique: Optional[str]) -> str:
    return technique if technique in CLOUD_MASK_TECHNIQUES else DEFAULT_CLOUD_MASK_TECHNIQUE


def build_s2_cloud_masked_collection(
    aoi,
    start_date: str,
    end_date: str,
    cloud_threshold: int,
    technique: str = DEFAULT_CLOUD_MASK_TECHNIQUE,
    cloud_prob_threshold: int = 40,
    collection_id: str = "COPERNICUS/S2_SR_HARMONIZED",
):
    """Scene-filtered (CLOUDY_PIXEL_PERCENTAGE) + per-pixel cloud-masked
    Sentinel-2 collection for the requested `technique`. Returns MASKED,
    UNSCALED (raw DN) images - callers apply their own reflectance scaling
    (divide by 10000) at whatever point in their pipeline they already do,
    same as before this existed. Masking is the only thing unified here.
    """
    technique = resolve_cloud_mask_technique(technique)
    base = (
        ee.ImageCollection(collection_id)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud_threshold))
    )

    if technique == "qa60":
        def _mask_qa60(image):
            qa = image.select("QA60")
            cloud = qa.bitwiseAnd(1 << 10).neq(0)
            cirrus = qa.bitwiseAnd(1 << 11).neq(0)
            return image.updateMask(cloud.Or(cirrus).Not())
        return base.map(_mask_qa60)

    if technique == "s2cloudless":
        prob_col = (
            ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY")
            .filterBounds(aoi)
            .filterDate(start_date, end_date)
        )
        joined = ee.Join.saveFirst("s2cloudless").apply(
            primary=base,
            secondary=prob_col,
            condition=ee.Filter.equals(leftField="system:index", rightField="system:index"),
        )

        def _mask_s2cloudless(image):
            image = ee.Image(image)
            prob = ee.Image(image.get("s2cloudless")).select("probability")
            return image.updateMask(prob.lt(cloud_prob_threshold))

        return ee.ImageCollection(joined).map(_mask_s2cloudless)

    # "scl" (default)
    def _mask_scl(image):
        scl = image.select("SCL")
        mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
        return image.updateMask(mask)

    return base.map(_mask_scl)


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
