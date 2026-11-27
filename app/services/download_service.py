"""GeoTIFF download URL generation (`/api/download/geotiff`).

Ported (business logic unchanged) from legacy `backend/app.py::download_geotiff`
(lines ~4094-4278). Returns a GEE-hosted download URL from
`image.getDownloadURL(...)` — never proxied file bytes.
"""
from __future__ import annotations

import logging
import math
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
    effective_year = year

    # ── Validasi dasar ───────────────────────────────────
    if start_month < 1 or start_month > 12:
        raise AnalysisError("start_month harus antara 1-12", 400)
    if end_month < 1 or end_month > 12:
        raise AnalysisError("end_month harus antara 1-12", 400)
    if start_month > end_month:
        raise AnalysisError("start_month tidak boleh lebih besar dari end_month", 400)
    if scale <= 0:
        raise AnalysisError("scale harus lebih besar dari 0", 400)

    year_min, year_max = (2001 if data.get("extension") else 2015), datetime.now(UTC).year
    if not (year_min <= year <= year_max):
        raise AnalysisError(f"Year harus antara {year_min}-{year_max}", 400)

    # Sanitasi filename sederhana
    filename = "".join(c for c in filename if c.isalnum() or c in ("_", "-", ".")).strip("._")
    if not filename:
        filename = f"savegeo_{layer_type}_{year}"

    # ── Build AOI ────────────────────────────────────────
    aoi = create_geometry_from_payload(aoi_spec)
    start_date, end_date = build_date_range(year, start_month, end_month)
    if end_month == 12:
        end_date = f"{year + 1}-01-01"

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
        effective_year = lc_meta.get("year", year)
        logger.info("Land cover export effective year for %s: %s", dataset, effective_year)
        filename = f"{filename}_{dataset}"

    # ── Carbon ──────────────────────────────────────────
    elif layer_type == "carbon":
        try:
            from app.registries.carbon_pool_models import get_builtin_carbon_model
            from app.services.carbon_pool_service import build_pool_export_image

            if data.get("extension"):
                from app.services.carbon_extensions import build_extension_image

                image, extension_meta = build_extension_image(data, aoi)
                image = image.clip(aoi)
                filename = f"{filename}_{data['extension']}"
                effective_year = extension_meta["year"]
            elif data.get("reference_dataset") and (data.get("reference_only") or get_builtin_carbon_model(model_name, db=db)):
                image = build_pool_export_image(db, data, aoi).clip(aoi).rename("carbon_Mg_per_ha")
                selected_model = get_builtin_carbon_model(model_name, db=db) if not data.get("reference_only") else None
                static_model = selected_model and selected_model["metadata_json"].get("method") in {"soil_stock_integration", "ipcc_root_to_shoot"}
                if data.get("reference_only") or static_model:
                    effective_year = int(data.get("dataset_year") or year)
                    filename = f"{filename}_reference_{effective_year}"
                filename = f"{filename}_carbon"
            else:
                image = _legacy_carbon_image(db, data, aoi, model_name, year, start_month, end_month, cloud_threshold)
                filename = f"{filename}_carbon"

        except AnalysisError:
            raise
        except ee.EEException:
            logger.exception("Earth Engine failed while preparing carbon GeoTIFF for AOI")
            raise
        except Exception as e:
            logger.exception("Carbon inference failed")
            raise AnalysisError("Model karbon tidak dapat diproses untuk AOI atau parameter ini.", 422) from e

    else:
        raise AnalysisError(f"Unknown layer_type: {layer_type}", 400)

    if image is None:
        raise AnalysisError("Failed to build image", 500)

    requested_scale = scale
    if data.get("export_mode") == "batch_gcs":
        from app.services.raster_batch_export import start_export

        return start_export(image, aoi, filename, scale, year, effective_year, data.get("_request_owner"))
    # Bound both uncompressed bytes and grid dimensions before requesting a
    # direct GEE download (32 MB / 10,000-pixel axis upstream limits).
    # Estimate the projected extent from the original GeoJSON where possible.
    # Asking Earth Engine for bounds here adds a synchronous EE request before
    # getDownloadURL and can fail independently (or consume quota) for large or
    # complex AOIs. The local estimate is deliberately conservative.
    dimensions = _projected_dimensions_from_payload(aoi_spec)
    width, height = dimensions or _projected_aoi_dimensions(aoi)
    bands = 3 if layer_type == "rgb" else 1
    safe_scale = max(scale, math.ceil(width / 9500), math.ceil(height / 9500), math.ceil(math.sqrt(width * height * bands * 8 / 24_000_000)))
    if safe_scale > scale:
        if not data.get("auto_scale", False):
            raise AnalysisError(f"AOI terlalu besar pada {scale} m. Gunakan minimal {safe_scale} m atau aktifkan resolusi aman otomatis.", 413)
        scale = safe_scale

    return _download_image(image, aoi, filename, layer_type, scale, requested_scale, effective_year, year, start_date, end_date)


def _projected_aoi_dimensions(aoi) -> tuple[float, float]:
    """Return the AOI bounding-box width and height in the export CRS (metres)."""
    projected = aoi.bounds(maxError=100, proj=ee.Projection("EPSG:6933")).coordinates().getInfo()[0]
    width = max(point[0] for point in projected) - min(point[0] for point in projected)
    height = max(point[1] for point in projected) - min(point[1] for point in projected)
    return width, height


def _projected_dimensions_from_payload(aoi_spec: dict) -> tuple[float, float] | None:
    """Estimate EPSG:6933 bounding dimensions without making an EE request."""
    positions: list[tuple[float, float]] = []

    def collect(node):
        if isinstance(node, dict):
            if "geojson" in node:
                collect(node["geojson"])
            elif node.get("type") == "FeatureCollection":
                for feature in node.get("features", []):
                    collect(feature)
            elif node.get("type") == "Feature":
                collect(node.get("geometry"))
            elif node.get("type") == "GeometryCollection":
                for geometry in node.get("geometries", []):
                    collect(geometry)
            elif "coordinates" in node:
                collect(node["coordinates"])
            elif {"west", "south", "east", "north"}.issubset(node):
                west, south, east, north = (float(node[key]) for key in ("west", "south", "east", "north"))
                positions.extend(((west, south), (west, north), (east, south), (east, north)))
        elif isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(value, (int, float)) and math.isfinite(value) for value in node[:2]):
                positions.append((float(node[0]), float(node[1])))
            else:
                for child in node:
                    collect(child)

    try:
        collect(aoi_spec)
    except (TypeError, ValueError, OverflowError):
        return None

    if len(positions) < 2:
        return None
    longitudes = [point[0] for point in positions]
    latitudes = [point[1] for point in positions]
    west, east = min(longitudes), max(longitudes)
    south, north = min(latitudes), max(latitudes)
    # A simple bbox becomes ambiguous for antimeridian-spanning geometries;
    # defer those uncommon cases to Earth Engine's geometry implementation.
    if east - west > 180 or south < -86 or north > 86:
        return None

    # EPSG:6933 is a cylindrical equal-area projection with a 30-degree
    # standard parallel. Use its spherical form for the export-size estimate;
    # the small safety factor protects against ellipsoid and boundary rounding.
    radius_m = 6_371_007.181
    standard_parallel_cos = math.cos(math.radians(30))

    def project(longitude, latitude):
        return (
            radius_m * standard_parallel_cos * math.radians(longitude),
            radius_m * math.sin(math.radians(latitude)) / standard_parallel_cos,
        )

    projected = [project(lon, lat) for lon in (west, east) for lat in (south, north)]
    width = (max(point[0] for point in projected) - min(point[0] for point in projected)) * 1.01
    height = (max(point[1] for point in projected) - min(point[1] for point in projected)) * 1.01
    if width <= 0 or height <= 0:
        return None
    return width, height


def _legacy_carbon_image(db, data, aoi, model_name, year, start_month, end_month, cloud_threshold):
    model_path = get_active_model_path(db, "carbon", model_name)
    inference_engine = CarbonInferenceEngine(
        model_name=model_name,
        model_path=model_path,
        quality_checks=False,
    )

    return (
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

def _download_image(image, aoi, filename, layer_type, scale, requested_scale, effective_year, year, start_date, end_date):
    try:
        # region lebih aman dikirim sebagai GeoJSON-like coordinates
        region_coords = aoi.bounds().getInfo()["coordinates"]

        download_url = image.clip(aoi).toFloat().getDownloadURL({
            "name": filename,
            "region": region_coords,
            "scale": scale,
            "crs": "EPSG:6933",
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
            "requested_scale": requested_scale,
            "scale_adjusted": scale != requested_scale,
            "crs": "EPSG:6933",
            "year": effective_year,
            "requested_year": year,
            "effective_year": effective_year,
            "date_range": {
                "start": start_date,
                "end": end_date
            }
        }

    except Exception as e:  # noqa: BLE001
        err = str(e)
        logger.error(f"getDownloadURL failed: {err}")

        lower_error = err.lower()
        if any(token in lower_error for token in (
            "429", "too many requests", "rate limit", "quota exceeded",
            "too many concurrent aggregations",
        )):
            raise AnalysisError(
                "Earth Engine sedang membatasi permintaan. Tunggu sebentar lalu ulangi ekspor.",
                429,
                extra={"suggestion": "Hindari menjalankan beberapa ekspor sekaligus."},
            ) from e

        if "too large" in lower_error or "pixel" in lower_error or "request payload size" in lower_error:
            raise AnalysisError(
                "Area terlalu besar untuk download langsung.",
                413,
                extra={"suggestion": f"Coba perbesar scale dari {scale}m atau perkecil AOI."},
            )

        raise AnalysisError("Gagal membuat URL unduhan GeoTIFF. Coba lagi atau hubungi administrator.", 500) from e
