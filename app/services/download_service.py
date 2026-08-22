"""GeoTIFF download URL generation (`/api/download/geotiff`).

Ported (business logic unchanged) from legacy `backend/app.py::download_geotiff`
(lines ~4094-4278). Returns a GEE-hosted download URL from
`image.getDownloadURL(...)` — never proxied file bytes.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

import ee
from sqlalchemy.orm import Session

from app.inference.carbon_inference import CarbonInferenceEngine
from app.registries.landcover_dataset_registry import LAND_COVER_LEGENDS
from app.registries.vegetation_index_registry import VEGETATION_INDEX_CATALOG as VEGETATION_INDICES
from app.repositories.uploaded_model_repo import get_active_model_path
from app.services import config_service
from app.services.gee_common import (
    AnalysisError,
    build_date_range,
    calculate_index,
    create_geometry_from_payload,
    mask_s2_clouds,
)
from app.services.landcover_service import get_landcover_image

logger = logging.getLogger(__name__)


def download_geotiff(db: Session, data: dict) -> dict:
    """
    Generate direct GeoTIFF download URL from Google Earth Engine.
    Catatan:
    - getDownloadURL() memiliki batas ukuran.
    - Untuk area besar, gunakan scale lebih besar atau AOI lebih kecil.
    """
    # ── Required ─────────────────────────────────────────
    aoi_spec = data.get("aoi")
    if not aoi_spec:
        raise AnalysisError("aoi is required", 400)

    # ── Inputs ───────────────────────────────────────────
    layer_type = str(data.get("layer_type", "vegetation")).strip().lower()
    year = int(data.get("year", datetime.now(UTC).year))
    start_month = int(data.get("start_month", 1))
    end_month = int(data.get("end_month", 12))
    cloud_threshold = int(data.get("cloud_threshold", config_service.get_analysis_defaults(db)["cloud_threshold"]))
    index_name = str(data.get("index_name", "NDVI")).strip().upper()
    dataset = str(data.get("dataset", "Dynamic_World")).strip()
    scale = int(data.get("scale", 30))
    model_name = data.get("model_name")
    filename = str(data.get("filename", f"savegeo_{layer_type}_{year}")).strip()

    # ── Validasi dasar ───────────────────────────────────
    if start_month < 1 or start_month > 12:
        raise AnalysisError("start_month harus antara 1-12", 400)
    if end_month < 1 or end_month > 12:
        raise AnalysisError("end_month harus antara 1-12", 400)
    if start_month > end_month:
        raise AnalysisError("start_month tidak boleh lebih besar dari end_month", 400)
    if scale <= 0:
        raise AnalysisError("scale harus lebih besar dari 0", 400)

    year_min, year_max = 2015, datetime.now(UTC).year
    if not (year_min <= year <= year_max):
        raise AnalysisError(f"Year harus antara {year_min}-{year_max}", 400)

    # Sanitasi filename sederhana
    filename = "".join(c for c in filename if c.isalnum() or c in ("_", "-", ".")).strip("._")
    if not filename:
        filename = f"savegeo_{layer_type}_{year}"

    # ── Build AOI ────────────────────────────────────────
    aoi = create_geometry_from_payload(aoi_spec)
    start_date, end_date = build_date_range(year, start_month, end_month)

    image = None

    # ── Vegetation index ────────────────────────────────
    if layer_type == "vegetation":
        if index_name not in VEGETATION_INDICES:
            raise AnalysisError(f"Unknown index: {index_name}", 400)

        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(aoi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud_threshold))
            .map(mask_s2_clouds)
        )

        if s2.size().getInfo() == 0:
            raise AnalysisError("Tidak ada citra Sentinel-2 untuk periode ini", 404)

        composite = s2.median().clip(aoi)
        image = calculate_index(composite, index_name).rename(index_name)
        filename = f"{filename}_{index_name}"

    # ── RGB ─────────────────────────────────────────────
    elif layer_type == "rgb":
        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(aoi)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud_threshold))
            .map(mask_s2_clouds)
        )

        if s2.size().getInfo() == 0:
            raise AnalysisError("Tidak ada citra Sentinel-2 untuk periode ini", 404)

        image = s2.median().select(["B4", "B3", "B2"]).clip(aoi)
        filename = f"{filename}_RGB"

    # ── Land cover ──────────────────────────────────────
    elif layer_type == "landcover":
        if dataset not in LAND_COVER_LEGENDS:
            raise AnalysisError(f"Unknown dataset: {dataset}", 400)
        image, lc_meta = get_landcover_image(dataset, year, aoi, start_month, end_month)
        logger.info(f"Land cover export effective year for {dataset}: {lc_meta.get('year')}")
        filename = f"{filename}_{dataset}"

    # ── Carbon ──────────────────────────────────────────
    elif layer_type == "carbon":
        try:
            model_path = get_active_model_path(db, "carbon", model_name)
            inference_engine = CarbonInferenceEngine(
                model_name=model_name,
                model_path=model_path
            )

            image = (
                inference_engine.predict_for_region(
                    roi=aoi,
                    year=year,
                    start_month=start_month,
                    end_month=end_month,
                    cloud_threshold=cloud_threshold
                )
                .clip(aoi)
                .rename("carbon_Mg_per_ha")
            )

            filename = f"{filename}_carbon"

        except Exception as e:
            logger.exception("Carbon inference failed")
            raise AnalysisError(f"Carbon inference failed: {e!s}", 400)

    else:
        raise AnalysisError(f"Unknown layer_type: {layer_type}", 400)

    if image is None:
        raise AnalysisError("Failed to build image", 500)

    logger.info(f"Generating GeoTIFF URL: {filename}, scale={scale}m")

    try:
        # region lebih aman dikirim sebagai GeoJSON-like coordinates
        region_coords = aoi.bounds().getInfo()["coordinates"]

        download_url = image.getDownloadURL({
            "name": filename,
            "region": region_coords,
            "scale": scale,
            "format": "GEO_TIFF",
            "filePerBand": False,
        })

        logger.info(f"Download URL generated for {filename}")

        return {
            "status": "success",
            "download_url": download_url,
            "filename": f"{filename}.tif",
            "layer_type": layer_type,
            "scale": scale,
            "year": year,
            "date_range": {
                "start": start_date,
                "end": end_date
            }
        }

    except Exception as e:  # noqa: BLE001
        err = str(e)
        logger.error(f"getDownloadURL failed: {err}")

        if "too large" in err.lower() or "pixel" in err.lower() or "request payload size" in err.lower():
            raise AnalysisError(
                "Area terlalu besar untuk download langsung.",
                413,
                extra={"suggestion": f"Coba perbesar scale dari {scale}m atau perkecil AOI.", "detail": err},
            )

        raise AnalysisError(f"Failed to generate download URL: {err}", 500)
