"""Bounded scientific operations for hyperspectral, thermal, InSAR and AI.

These endpoints deliberately separate scientific raster operations from map
tiles.  They require explicit metadata and return an unavailable status when
an optional processor is not installed; no result is labelled as calibrated or
InSAR unless the required inputs and processor are present.
"""
from __future__ import annotations

import importlib.util
import math
from typing import Any

import numpy as np
import rasterio
from rasterio.warp import transform as transform_coordinates

from app.services.gee_common import AnalysisError


def _optional_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def capabilities() -> dict[str, Any]:
    has_samgeo = _optional_module("samgeo") or _optional_module("segment_anything")
    has_insar = _optional_module("isce3") or _optional_module("pyrosar") or _optional_module("snappy")
    return {
        "hyperspectral": {
            "status": "ready" if _optional_module("rasterio") else "missing_reader",
            "operations": ["spectral_profile", "pixel_inspector", "custom_rgb"],
            "max_bands_per_request": 256,
        },
        "thermal": {
            "status": "ready",
            "operations": ["scale_offset", "kelvin_to_celsius"],
            "requires_scale_offset": True,
        },
        "insar": {
            "status": "ready" if has_insar else "processor_missing",
            "processor": "isce3/pyroSAR/SNAP" if has_insar else None,
            "operations": ["validate_pair", "coherence", "interferogram", "terrain_correction"],
            "requires_slc_pair": True,
        },
        "ai": {
            "status": "ready" if has_samgeo else "model_runtime_optional",
            "operations": ["segmentation", "classification", "change_detection"],
            "server_side_only": True,
        },
    }


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AnalysisError(f"{label} harus berupa angka", 400) from exc
    if not math.isfinite(number):
        raise AnalysisError(f"{label} harus finite", 400)
    return number


def thermal_calibration(data: dict[str, Any]) -> dict[str, Any]:
    """Apply explicit product scale/offset to supplied DN values.

    The endpoint refuses to guess a sensor calibration.  `scale` and `offset`
    must come from the product metadata; Kelvin values are converted to °C
    only when the caller explicitly declares the source unit.
    """
    if not isinstance(data.get("values"), list) or len(data["values"]) > 100_000:
        raise AnalysisError("values harus berupa array maksimal 100.000 angka", 400)
    if "scale" not in data or "offset" not in data:
        raise AnalysisError("Thermal calibration membutuhkan scale dan offset dari metadata produk", 400)
    scale = _finite_number(data["scale"], "scale")
    offset = _finite_number(data["offset"], "offset")
    source_unit = str(data.get("source_unit") or "DN").strip().upper()
    output_unit = str(data.get("output_unit") or source_unit).strip().upper()
    if source_unit not in {"DN", "K", "C", "CELSIUS", "KELVIN"}:
        raise AnalysisError("source_unit harus DN, K, atau C", 400)
    calibrated = [(_finite_number(value, "nilai") * scale) + offset for value in data["values"]]
    if source_unit in {"K", "KELVIN"} and output_unit in {"C", "CELSIUS"}:
        calibrated = [value - 273.15 for value in calibrated]
        output_unit = "C"
    return {
        "values": calibrated,
        "scale": scale,
        "offset": offset,
        "source_unit": source_unit,
        "output_unit": output_unit,
        "formula": "(DN * scale) + offset" + (" - 273.15" if source_unit in {"K", "KELVIN"} and output_unit == "C" else ""),
        "calibrated": True,
        "warning": "Pastikan scale/offset berasal dari metadata produk thermal yang dipilih.",
    }


def spectral_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Read one pixel spectrum from a COG/STAC asset without creating a tile."""
    item_url = str(data.get("item_url") or "").strip()
    asset_key = str(data.get("asset_key") or "visual").strip()
    provider_key = str(data.get("provider_key") or "").strip() or None
    try:
        longitude = _finite_number(data.get("longitude"), "longitude")
        latitude = _finite_number(data.get("latitude"), "latitude")
    except AnalysisError:
        raise
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise AnalysisError("Koordinat pixel berada di luar rentang WGS84", 400)
    if not item_url:
        raise AnalysisError("item_url diperlukan untuk spectral profile", 400)

    # Import lazily to keep the adapter boundary clear and avoid a module
    # import cycle during FastAPI startup.
    from app.services import imagery_service

    href = imagery_service._readable_stac_asset_href(item_url, asset_key, provider_key)
    with rasterio.open(href) as source:
        if source.crs is None:
            raise AnalysisError("Asset hyperspectral tidak memiliki CRS", 422)
        source_x, source_y = transform_coordinates("EPSG:4326", source.crs, [longitude], [latitude])
        sample = next(source.sample([(source_x[0], source_y[0])], indexes=list(range(1, source.count + 1)), masked=True))
        values = [None if np.ma.is_masked(value) else float(value) for value in sample]
        descriptions = list(source.descriptions or ())
        tags = source.tags()
        wavelengths = data.get("wavelengths_nm") or tags.get("wavelengths_nm")
        if isinstance(wavelengths, str):
            try:
                wavelengths = [float(part.strip()) for part in wavelengths.split(",") if part.strip()]
            except ValueError as exc:
                raise AnalysisError("wavelengths_nm tidak valid", 400) from exc
        if wavelengths is not None and len(wavelengths) != len(values):
            raise AnalysisError("Jumlah wavelength harus sama dengan jumlah band", 400)
    return {
        "longitude": longitude,
        "latitude": latitude,
        "bands": [
            {"band": index + 1, "value": value, "label": descriptions[index] or f"Band {index + 1}", "wavelength_nm": wavelengths[index] if wavelengths else None}
            for index, value in enumerate(values)
        ],
        "wavelengths_available": bool(wavelengths),
        "source_asset": asset_key,
    }


def validate_insar_pair(data: dict[str, Any]) -> dict[str, Any]:
    first = data.get("master") or {}
    second = data.get("slave") or {}
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise AnalysisError("master dan slave harus berupa metadata scene", 400)
    master_id, slave_id = str(first.get("id") or ""), str(second.get("id") or "")
    if not master_id or not slave_id or master_id == slave_id:
        raise AnalysisError("Pilih dua scene SLC yang berbeda", 400)
    product_types = {str(first.get("product_type") or "").upper(), str(second.get("product_type") or "").upper()}
    if not all("SLC" in product_type for product_type in product_types):
        raise AnalysisError("InSAR membutuhkan pasangan produk Sentinel-1/ICEYE SLC", 400)
    capabilities_payload = capabilities()["insar"]
    return {
        "master_id": master_id,
        "slave_id": slave_id,
        "ready": capabilities_payload["status"] == "ready",
        "status": capabilities_payload["status"],
        "processor": capabilities_payload["processor"],
        "next_operations": capabilities_payload["operations"],
        "warning": "Validasi pasangan tidak menjalankan interferogram. Pastikan orbit, polarisasi, baseline, DEM, dan lisensi processor sesuai sebelum submit job.",
    }
