"""IBM Granite geospatial biomass inference on NASA HLS L30 composites.

This optional path is intentionally isolated from the existing Sentinel-2 and
STAC estimators. It follows IBM's published TerraTorch inference entry point,
uses the training band order/normalization from the supplied official config,
and reports carbon density only after converting predicted AGB by 0.47.
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
import threading
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np
import requests
from sqlalchemy.orm import Session

from app.registries.granite_biomass_registry import (
    MODEL_NAME,
    TARGET_DATASET,
    granite_biomass_paths,
)
from app.services import config_service
from app.services.gee_common import AnalysisError, create_geometry_from_payload, geometry_area_ha

logger = logging.getLogger(__name__)

HLS_COLLECTION = "NASA/HLS/HLSL30/v002"
HLS_SOURCE_BANDS = ("B2", "B3", "B4", "B5", "B6", "B7")
MODEL_BANDS = ("BLUE", "GREEN", "RED", "NIR_NARROW", "SWIR_1", "SWIR_2")
PREDICT_DATASET_BANDS = ["-1", *MODEL_BANDS, "-1", "-1", "-1", "-1"]
CARBON_FRACTION = 0.47
MODEL_SCALE_M = 30
MAX_AOI_AREA_HA = 25_000  # 250 km²; keeps EE transfer and local inference bounded.
MAX_RASTER_PIXELS = 300_000
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
_MODEL_LOCK = threading.RLock()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _load_model_cached(
    checkpoint_path: str,
    config_path: str,
    expected_sha256: str,
    checkpoint_size: int,
    checkpoint_mtime_ns: int,
):
    del checkpoint_size, checkpoint_mtime_ns  # included in cache key to reload replaced files
    checkpoint = Path(checkpoint_path)
    if _sha256(checkpoint) != expected_sha256:
        raise AnalysisError(
            "Checkpoint IBM Granite tidak lolos verifikasi SHA-256. Periksa konfigurasi administrator.",
            503,
        )

    try:
        from terratorch.cli_tools import LightningInferenceModel

        model = LightningInferenceModel.from_config(
            config_path,
            checkpoint_path,
            PREDICT_DATASET_BANDS,
        )
        model.eval()
        return model
    except AnalysisError:
        raise
    except Exception as exc:  # noqa: BLE001 - upstream model load details stay server-side
        logger.exception("Could not load configured IBM Granite biomass model")
        raise AnalysisError("Model IBM Granite belum dapat dimuat. Periksa runtime dan berkas model di server.", 503) from exc


def _get_model():
    paths = granite_biomass_paths()
    if paths is None:
        raise AnalysisError(
            "Model IBM Granite belum diaktifkan atau runtime/checkpoint belum lengkap di server.",
            503,
        )
    checkpoint, config, expected_sha256 = paths
    stat = checkpoint.stat()
    return _load_model_cached(
        str(checkpoint), str(config), expected_sha256, stat.st_size, stat.st_mtime_ns
    )


def _analysis_period(data: dict[str, Any]) -> tuple[int, int, int, str, str]:
    from datetime import UTC, datetime

    now_year = datetime.now(UTC).year
    try:
        year = int(data.get("year", now_year))
        start_month = int(data.get("start_month", 1))
        end_month = int(data.get("end_month", 12))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AnalysisError("Tahun dan bulan analisis harus berupa bilangan bulat.", 400) from exc
    if year < 2013 or year > now_year or not (1 <= start_month <= end_month <= 12):
        raise AnalysisError("Rentang waktu HLS harus valid dan berada antara 2013 hingga tahun berjalan.", 400)
    start_date = f"{year}-{start_month:02d}-01"
    if end_month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{end_month + 1:02d}-01"
    return year, start_month, end_month, start_date, end_date


def _hls_composite(aoi, start_date: str, end_date: str):
    import ee

    def clear_land(image):
        fmask = image.select("Fmask")
        # Fmask bits 1..5 mean cloud, cloud adjacency, shadow, snow/ice, water.
        invalid_bits = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
        valid = fmask.bitwiseAnd(invalid_bits).eq(0)
        return image.updateMask(valid).select(HLS_SOURCE_BANDS, MODEL_BANDS)

    collection = (
        ee.ImageCollection(HLS_COLLECTION)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .map(clear_land)
    )
    count = int(collection.size().getInfo())
    if count == 0:
        raise AnalysisError("Tidak ada citra NASA HLS L30 pada AOI dan periode yang dipilih.", 422)

    composite = collection.median().select(MODEL_BANDS)
    # HLS reflectance is stored in 0..1 units; IBM's training config expects
    # surface-reflectance integer values in the 0..10000 range.
    scaled = composite.multiply(10_000).toInt16().clip(aoi)
    common_valid = scaled.mask().reduce(ee.Reducer.min()).rename("valid").unmask(0)
    return scaled.addBands(common_valid).unmask(0), count


def _download_hls_raster(image, aoi, destination: Path) -> tuple[Path, int]:
    import rasterio

    try:
        url = image.getDownloadURL({
            "bands": [*MODEL_BANDS, "valid"],
            "region": aoi,
            "scale": MODEL_SCALE_M,
            "format": "GEO_TIFF",
            "filePerBand": False,
        })
        response = requests.get(url, stream=True, timeout=(20, 180))
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise AnalysisError("Raster HLS melebihi batas unduhan aman untuk satu analisis.", 413)
        payload = bytes(content)
    except AnalysisError:
        raise
    except requests.RequestException as exc:
        logger.warning("IBM Granite HLS download failed (%s)", type(exc).__name__)
        raise AnalysisError("Data NASA HLS sementara tidak dapat diunduh dari Earth Engine.", 503) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("IBM Granite HLS export failed (%s)", type(exc).__name__)
        raise AnalysisError("Earth Engine tidak dapat menyiapkan komposit HLS untuk model IBM Granite.", 422) from exc

    if zipfile.is_zipfile(io.BytesIO(payload)):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                geotiffs = [name for name in archive.namelist() if name.lower().endswith((".tif", ".tiff"))]
                if len(geotiffs) != 1:
                    raise AnalysisError("Earth Engine tidak mengembalikan satu raster HLS gabungan.", 422)
                if archive.getinfo(geotiffs[0]).file_size > MAX_DOWNLOAD_BYTES:
                    raise AnalysisError("Raster HLS terkompresi melebihi batas unduhan aman.", 413)
                # Read only the expected single TIFF; never extract untrusted ZIP paths.
                payload = archive.read(geotiffs[0])
        except AnalysisError:
            raise
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise AnalysisError("Berkas raster HLS dari Earth Engine tidak valid.", 422) from exc

    if not payload or len(payload) > MAX_DOWNLOAD_BYTES:
        raise AnalysisError("Ukuran raster HLS tidak valid atau melebihi batas aman.", 413)
    raster_path = destination / "granite_hls_input.tif"
    raster_path.write_bytes(payload)
    try:
        with rasterio.open(raster_path) as source:
            if source.count != 7 or source.width < 1 or source.height < 1:
                raise AnalysisError("Raster HLS harus berisi enam band model dan satu mask validitas.", 422)
            if source.width * source.height > MAX_RASTER_PIXELS:
                raise AnalysisError("AOI menghasilkan terlalu banyak piksel untuk inferensi IBM Granite.", 413)
    except AnalysisError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AnalysisError("Raster HLS dari Earth Engine tidak dapat dibaca.", 422) from exc
    return raster_path, len(payload)


def _prepare_model_raster(source_path: Path, prediction_dir: Path):
    import rasterio

    input_path = prediction_dir / "hls_l30_aoi.tif"
    with rasterio.open(source_path) as source:
        spectral = source.read((1, 2, 3, 4, 5, 6))
        valid = source.read(7) > 0
        # Match the IBM notebook's 11-channel dataset_bands ordering. Channels
        # marked "-1" are deliberately ignored by TerraTorch's band selector.
        stack = np.zeros((11, source.height, source.width), dtype=np.int16)
        stack[1:7] = spectral
        profile = source.profile.copy()
        profile.update(count=11, dtype="int16", nodata=0, compress="deflate", predictor=2)
        with rasterio.open(input_path, "w", **profile) as target:
            target.write(stack)
    return input_path, valid


def _prediction_array(result: Any) -> np.ndarray:
    value = result
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    array = np.squeeze(array)
    if array.ndim != 2:
        raise AnalysisError("Format keluaran IBM Granite tidak sesuai untuk raster biomassa.", 422)
    return array.astype(np.float32, copy=False)


def _pixel_area_m2(source) -> float:
    from pyproj import CRS, Geod, Transformer

    transform = source.transform
    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    crs = CRS.from_user_input(source.crs) if source.crs else None
    if crs and crs.is_projected:
        units_to_m = crs.axis_info[0].unit_conversion_factor or 1.0
        return pixel_area * units_to_m * units_to_m
    if not crs:
        raise AnalysisError("Raster HLS tidak memiliki sistem koordinat yang dapat dihitung.", 422)
    to_wgs84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    col, row = source.width / 2, source.height / 2
    corners = [transform * (col, row), transform * (col + 1, row),
               transform * (col + 1, row + 1), transform * (col, row + 1)]
    lons, lats = zip(*(to_wgs84.transform(x, y) for x, y in corners))
    area, _ = Geod(ellps="WGS84").polygon_area_perimeter(lons, lats)
    return abs(area)


def analyze_granite_biomass(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    if "aoi" not in data:
        raise AnalysisError("Missing required field: aoi", 400)
    try:
        aoi = create_geometry_from_payload(data["aoi"])
        original_area_ha = geometry_area_ha(aoi)
    except (ValueError, TypeError) as exc:
        raise AnalysisError(str(exc), 400) from exc
    if not math.isfinite(original_area_ha) or original_area_ha <= 0:
        raise AnalysisError("AOI harus memiliki luas lebih dari nol.", 400)
    if original_area_ha > MAX_AOI_AREA_HA:
        raise AnalysisError("Model IBM Granite dibatasi untuk AOI hingga 250 km² per analisis.", 413)

    year, start_month, end_month, start_date, end_date = _analysis_period(data)
    started = time.perf_counter()
    import ee

    try:
        composite, images_used = _hls_composite(aoi, start_date, end_date)
    except AnalysisError:
        raise
    except ee.EEException as exc:
        logger.warning("IBM Granite HLS collection failed (%s)", type(exc).__name__)
        raise AnalysisError("Earth Engine gagal membaca NASA HLS untuk AOI atau periode ini.", 422) from exc

    with TemporaryDirectory(prefix="savegeo-granite-") as workdir:
        work_path = Path(workdir)
        raster_path, _download_size = _download_hls_raster(composite, aoi, work_path)
        input_dir = work_path / "input"
        input_dir.mkdir()
        model_input, valid = _prepare_model_raster(raster_path, input_dir)
        if not valid.any():
            raise AnalysisError("Tidak ada piksel daratan HLS yang valid setelah penyaringan awan dan kualitas.", 422)
        if int(valid.sum()) > MAX_RASTER_PIXELS:
            raise AnalysisError("AOI menghasilkan terlalu banyak piksel untuk inferensi IBM Granite.", 413)

        try:
            import torch

            with _MODEL_LOCK:
                model = _get_model()
                with torch.inference_mode():
                    results, filenames = model.inference_on_dir(str(input_dir))
        except AnalysisError:
            raise
        except Exception as exc:  # noqa: BLE001 - never expose checkpoint internals
            logger.exception("IBM Granite inference failed")
            raise AnalysisError("Inferensi IBM Granite gagal. Periksa kompatibilitas runtime TerraTorch di server.", 503) from exc
        if len(results) != 1 or len(filenames) != 1:
            raise AnalysisError("TerraTorch tidak mengembalikan tepat satu hasil untuk AOI.", 422)
        returned_name = Path(str(filenames[0])).name
        if returned_name and returned_name != model_input.name:
            logger.warning("TerraTorch returned unexpected input filename for IBM Granite inference")
        agb = _prediction_array(results[0])

        import rasterio

        with rasterio.open(raster_path) as source:
            if agb.shape != (source.height, source.width):
                raise AnalysisError("Dimensi hasil IBM Granite tidak cocok dengan grid HLS sumber.", 422)
            pixel_area_m2 = _pixel_area_m2(source)
            if not math.isfinite(pixel_area_m2) or pixel_area_m2 <= 0:
                raise AnalysisError("Resolusi raster HLS tidak valid untuk menghitung stok karbon.", 422)

        valid &= np.isfinite(agb)
        if not valid.any():
            raise AnalysisError("IBM Granite tidak menghasilkan nilai biomassa valid pada AOI.", 422)
        # The configured model head is ReLU; clipping tiny negative numerical
        # values preserves its non-negative AGB definition.
        carbon = np.maximum(agb[valid], 0.0) * CARBON_FRACTION
        mean_carbon = float(np.mean(carbon, dtype=np.float64))
        std_carbon = float(np.std(carbon, dtype=np.float64))
        min_carbon = float(np.min(carbon))
        max_carbon = float(np.max(carbon))
        valid_pixels = int(carbon.size)
        valid_area_ha = valid_pixels * pixel_area_m2 / 10_000

    analysis_defaults = config_service.get_analysis_defaults(db)
    co2_factor = float(analysis_defaults["carbon_co2_factor"])
    total_carbon_tons = mean_carbon * valid_area_ha
    vis_min = float(data.get("vis_min", 0))
    vis_max = float(data.get("vis_max", 200))
    vis_palette = data.get("vis_palette", ["#ffffcc", "#c2e699", "#78c679", "#238443"])
    model_meta = {
        "provider": "ibm_granite_hls",
        "target_dataset_key": TARGET_DATASET,
        "input_dataset": HLS_COLLECTION,
        "input_bands": list(MODEL_BANDS),
        "training_reference": "NASA GEDI L4A",
        "carbon_fraction": CARBON_FRACTION,
        "input_scale_m": MODEL_SCALE_M,
    }
    return {
        "carbon_estimated": {
            "tile_url": None,
            "statistics": {
                "mean": round(mean_carbon, 2),
                "std_dev": round(std_carbon, 2),
                "min": round(min_carbon, 2),
                "max": round(max_carbon, 2),
                "n_pixels": valid_pixels,
            },
            "unit": "Mg C/ha",
            "target_pool": "aboveground_biomass_carbon",
            "vis_params": {"min": vis_min, "max": vis_max, "palette": vis_palette},
            "inference_mode": "ibm_granite_hls_stats",
        },
        "carbon_reference": {
            "tile_url": None,
            "name": "GEDI L4A (sumber label pelatihan model)",
            "full_name": "NASA GEDI Level 4A; provenance pelatihan, bukan observasi referensi AOI ini",
            "resolution": 25,
            "statistics": None,
            "description": "Model memprediksi AGB dari NASA HLS L30; GEDI L4A hanya sumber label pelatihan.",
        },
        "area_info": {
            "calculation_mode": "valid_hls_pixels",
            "original_aoi_area_ha": round(original_area_ha, 2),
            "filtering_area_ha": round(valid_area_ha, 2),
            "calculation_area_ha": round(valid_area_ha, 2),
            "total_carbon_tons": round(total_carbon_tons, 2),
            "carbon_dioxide_equivalent_tons": round(total_carbon_tons * co2_factor, 2),
        },
        "model_info": {
            "model_name": MODEL_NAME,
            "algorithm": "TerraTorch pixel-wise regression",
            "version": "1",
            "calculation_mode": "clipped_aoi",
            "reference_dataset": TARGET_DATASET,
            "analysis_year": year,
            "inference_date_range": f"{start_date} to {end_date}",
            "scale": MODEL_SCALE_M,
            "images_used": images_used,
            "co2_conversion_factor": co2_factor,
            "gee_deployable": False,
            "statistics_available": True,
            "provider": "ibm_granite_hls",
            "carbon_fraction": CARBON_FRACTION,
            "training_reference": "NASA GEDI L4A",
            "accuracy_status": "global_zero_shot; local_field_validation_required",
            "caveat_note": (
                "Estimasi eksperimental dari AGB global IBM Granite; dikonversi ke karbon dengan faktor 0,47. "
                "Belum divalidasi terhadap pengukuran lapangan Indonesia. Hasil statistik saja; peta prediksi per piksel belum disajikan."
            ),
            "validation_note": (
                "Estimasi eksperimental zero-shot dari model global IBM Granite. Hasil ini belum divalidasi "
                "dengan pengukuran lapangan di Indonesia; GEDI L4A adalah sumber label pelatihan, bukan "
                "validasi untuk AOI ini."
            ),
            "method_description": (
                "Komposit median NASA HLS L30 dengan band Blue, Green, Red, NIR, SWIR1, SWIR2; "
                "Fmask mengeluarkan awan, bayangan, salju, area berdekatan awan, dan air. "
                "Prediksi AGB dikalikan 0,47 untuk memperoleh Mg C/ha."
            ),
            "month_range": f"{start_month}-{end_month}",
            **model_meta,
        },
        "data_quality": {
            "valid_pixel_pct": round(min(100.0, valid_area_ha / original_area_ha * 100), 2),
            "gap_filled": False,
            "images_used": images_used,
            "coefficient_of_variation_pct": round(std_carbon / mean_carbon * 100, 2) if mean_carbon else None,
            "model_r2": None,
            "model_rmse": None,
            "cloud_mask_technique": "HLS Fmask bits 1-5 (water/cloud/adjacency/shadow/snow excluded)",
        },
        "processing_time": round(time.perf_counter() - started, 1),
    }
