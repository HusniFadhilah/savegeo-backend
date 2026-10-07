"""Serve validated, locally archived CAMS fields; HTTP reads never start ADS jobs."""

from __future__ import annotations

import io
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import COGReader
from rasterio import open as raster_open
from rasterio.warp import transform_geom
from rasterio.windows import Window, from_bounds
from shapely.geometry import Polygon, box, mapping, shape

from app.core.config import get_settings
from app.services import cams_emissions_service
from app.services.cams_emissions_service import CamsRequestError

MAX_AGGREGATION_CELLS = 100_000
MAX_GRID_FEATURES = 5_000
MAX_CUSTOM_GEOMETRY_VERTICES = 10_000

LAYER_DEFINITIONS = {
    "burnt_carbon": {
        "product": "gfas",
        "variable": "burnt_carbon",
        "source_variable": "cfire",
        "source_unit": "kg m-2 s-1",
        "display_unit": "t C/km²/day",
        "multiplier": 86_400_000.0,
        "palette": ["542400", "ba5100", "ff9300", "ffe033", "fffbd6"],
    },
    "organic_aod": {
        "product": "ifs_cams",
        "variable": "organic_aod",
        "source_variable": "omaod550",
        "source_unit": "1",
        "display_unit": "AOD 550 nm",
        "multiplier": 1.0,
        "palette": ["e8dde8", "eca4ca", "e648a7", "a318a6", "40006b"],
    },
}


def event_directory(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,179}", slug):
        raise CamsRequestError("Identitas kejadian tidak valid")
    return Path(get_settings().cams_archive_dir).resolve() / slug


def read_archive(slug: str) -> dict[str, Any]:
    directory = event_directory(slug)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CamsRequestError(
            "Arsip CAMS untuk kejadian ini belum tersedia", 503, "data_unavailable"
        ) from None
    except (OSError, ValueError):
        raise CamsRequestError("Arsip CAMS tidak dapat dibaca", 503, "archive_invalid") from None
    if manifest.get("schema_version") != 1 or manifest.get("event_slug") != slug:
        raise CamsRequestError("Provenans arsip CAMS tidak sesuai", 503, "archive_invalid")
    for layer_id, layer in manifest.get("layers", {}).items():
        if layer_id not in LAYER_DEFINITIONS:
            raise CamsRequestError("Variabel arsip CAMS tidak dikenal", 503, "archive_invalid")
        if not layer.get("frames"):
            continue
        if not isinstance(layer.get("maximum"), (float, int)) or not np.isfinite(layer["maximum"]):
            raise CamsRequestError("Rentang layer CAMS tidak valid", 503, "archive_invalid")
        for frame in layer["frames"]:
            filename = frame.get("filename", "")
            if not re.fullmatch(r"[a-f0-9]{24}\.tif", filename):
                raise CamsRequestError("Berkas arsip CAMS tidak valid", 503, "archive_invalid")
            if not (directory / filename).is_file() or not frame.get("sha256"):
                raise CamsRequestError("Berkas arsip CAMS tidak lengkap", 503, "archive_invalid")
    return manifest


def public_archive(slug: str) -> dict[str, Any]:
    manifest = read_archive(slug)
    return {
        **{k: v for k, v in manifest.items() if k != "layers"},
        "layers": {
            key: {
                **layer,
                "frames": [{k: v for k, v in frame.items() if k != "filename"} for frame in layer["frames"]],
            }
            for key, layer in manifest["layers"].items()
        },
    }


def capabilities(slug: str) -> dict[str, Any]:
    result = cams_emissions_service.get_capabilities()
    try:
        manifest = read_archive(slug)
        available = any(layer.get("frames") for layer in manifest.get("layers", {}).values())
    except CamsRequestError as exc:
        return result if exc.code == "data_unavailable" else {**result, "blocked_reason": exc.code}
    if not available:
        return result
    return {
        **result,
        "status": "ready",
        "provider_ready": True,
        "data_available": True,
        "blocked_reason": None,
        "integration_stage": "validated_ads_archive",
        "archive_updated_at": manifest["updated_at"],
        "available_layers": list(manifest["layers"]),
        "disclaimer": "GFAS estimates fire-source emissions; IFS-CAMS organic aerosol includes multiple sources.",
    }


def resolve_frame(slug: str, layer_id: str, frame_id: str) -> tuple[dict, dict, Path]:
    manifest = read_archive(slug)
    layer = manifest.get("layers", {}).get(layer_id)
    if not layer:
        raise CamsRequestError("Layer CAMS belum tersedia", 404, "layer_not_found")
    frame = next((f for f in layer["frames"] if f["id"] == frame_id), None)
    if not frame:
        raise CamsRequestError("Waktu CAMS belum tersedia", 404, "frame_not_found")
    return layer, frame, event_directory(slug) / frame["filename"]


def request_summary(slug: str, request: dict[str, str], region_code: str | None = None) -> dict:
    manifest = read_archive(slug)
    for layer_id, layer in manifest["layers"].items():
        definition = LAYER_DEFINITIONS[layer_id]
        if definition["product"] != request["product"] or definition["variable"] != request["variable"]:
            continue
        for frame in layer["frames"]:
            if frame["valid_at"] == request["valid_at"] and frame["version"] == request["version"]:
                summary = frame["summary"]
                if region_code:
                    match = next((r for r in frame["regions"] if r["code"] == region_code), None)
                    if match is None:
                        raise CamsRequestError(
                            "Wilayah berada di luar arsip kejadian", 400, "region_not_available"
                        )
                    summary = match["summary"]
                return {
                    "request": request,
                    "summary": summary,
                    "regions": frame["regions"],
                    "provenance": frame["provenance"],
                }
    raise CamsRequestError("Data CAMS belum diarsipkan untuk waktu ini", 503, "data_unavailable")


def tile(slug: str, layer_id: str, frame_id: str, z: int, x: int, y: int) -> bytes:
    if not 0 <= z <= 14 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise CamsRequestError("Koordinat tile tidak valid")
    layer, _, path = resolve_frame(slug, layer_id, frame_id)
    blank = np.zeros((256, 256, 4), dtype=np.uint8)
    try:
        with COGReader(str(path)) as reader:
            image = reader.tile(x, y, z, tilesize=256, resampling_method="nearest")
    except TileOutsideBounds:
        result = blank
    except Exception:
        raise CamsRequestError("Raster CAMS tidak dapat dimuat", 503, "raster_unavailable") from None
    else:
        values = image.data[0].astype(float) * LAYER_DEFINITIONS[layer_id]["multiplier"]
        valid = (image.mask > 0) & np.isfinite(values) & (values > 0)
        maximum = max(float(layer["maximum"]), float(np.finfo(float).tiny))
        positions = np.linspace(0, maximum, len(layer["palette"]))
        colors = np.array([[int(c[i : i + 2], 16) for i in (0, 2, 4)] for c in layer["palette"]])
        result = blank
        for channel in range(3):
            result[:, :, channel] = np.interp(
                np.where(valid, values, 0), positions, colors[:, channel]
            ).astype(np.uint8)
        result[:, :, 3] = np.where(valid, 255, 0).astype(np.uint8)
    output = io.BytesIO()
    Image.fromarray(result).save(output, format="PNG")
    return output.getvalue()


def aggregate(
    slug: str,
    layer_id: str,
    frame_id: str,
    mode: str,
    *,
    geometry: dict[str, Any] | None = None,
    bbox: list[float] | None = None,
) -> dict[str, Any]:
    """Calculate raster statistics for a user polygon or visible native grid cells."""
    if mode not in {"area", "grid"}:
        raise CamsRequestError("Mode agregasi CAMS tidak valid")
    manifest = read_archive(slug)
    layer = manifest.get("layers", {}).get(layer_id)
    if not layer:
        raise CamsRequestError("Layer CAMS belum tersedia", 404, "layer_not_found")
    frame = next((item for item in layer["frames"] if item["id"] == frame_id), None)
    if not frame:
        raise CamsRequestError("Waktu CAMS belum tersedia", 404, "frame_not_found")
    path = event_directory(slug) / frame["filename"]
    event_box = box(*manifest["bbox"])

    if mode == "area":
        candidate = geometry.get("geometry", geometry) if isinstance(geometry, dict) else None
        try:
            target = shape(candidate)
        except Exception as exc:
            raise CamsRequestError("Geometri area kustom tidak valid") from exc
        if target.geom_type not in {"Polygon", "MultiPolygon"} or not target.is_valid:
            raise CamsRequestError("Area kustom harus berupa polygon yang valid")
        if _geometry_vertex_count(candidate) > MAX_CUSTOM_GEOMETRY_VERTICES:
            raise CamsRequestError("Area kustom memiliki terlalu banyak titik")
        target = target.intersection(event_box)
    else:
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise CamsRequestError("Batas peta diperlukan untuk agregasi grid")
        try:
            west, south, east, north = (float(value) for value in bbox)
        except (TypeError, ValueError) as exc:
            raise CamsRequestError("Batas peta tidak valid") from exc
        if not all(math.isfinite(value) for value in (west, south, east, north)) or not (
            -180 <= west < east <= 180 and -90 <= south < north <= 90
        ):
            raise CamsRequestError("Batas peta tidak valid")
        target = box(west, south, east, north).intersection(event_box)

    if target.is_empty or target.area <= 0:
        raise CamsRequestError("Area agregasi berada di luar cakupan event")

    try:
        with raster_open(path) as dataset:
            if dataset.crs is None:
                raise CamsRequestError("CRS raster CAMS tidak tersedia", 503, "raster_invalid")
            raster_geometry = shape(transform_geom("EPSG:4326", dataset.crs, mapping(target)))
            window = from_bounds(*raster_geometry.bounds, transform=dataset.transform)
            col_start = max(0, math.floor(window.col_off))
            row_start = max(0, math.floor(window.row_off))
            col_stop = min(dataset.width, math.ceil(window.col_off + window.width))
            row_stop = min(dataset.height, math.ceil(window.row_off + window.height))
            width, height = col_stop - col_start, row_stop - row_start
            if width <= 0 or height <= 0:
                return _empty_aggregate(layer, mode)
            if width * height > MAX_AGGREGATION_CELLS:
                raise CamsRequestError(
                    "Area agregasi terlalu luas; perbesar peta atau gambar area yang lebih kecil",
                    413,
                    "aggregation_too_large",
                )
            read_window = Window(col_start, row_start, width, height)
            values = dataset.read(1, window=read_window, masked=True)
            transform = dataset.window_transform(read_window)
            multiplier = LAYER_DEFINITIONS[layer_id]["multiplier"]
            palette_maximum = max(float(layer.get("maximum", 0)), float(np.finfo(float).tiny))
            valid_values: list[float] = []
            area_weighted_sum = 0.0
            total_area = 0.0
            integrated_mass_kg = 0.0
            missing_cells = 0
            features: list[dict[str, Any]] = []
            duration_seconds = _frame_duration_seconds(frame)

            for row in range(height):
                for col in range(width):
                    x0, y0 = transform * (col, row)
                    x1, y1 = transform * (col + 1, row + 1)
                    cell = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
                    clipped = cell.intersection(raster_geometry)
                    if clipped.is_empty or clipped.area <= 0:
                        continue
                    raw = float(values[row, col]) if not np.ma.is_masked(values[row, col]) else float("nan")
                    if not math.isfinite(raw) or raw < 0:
                        missing_cells += 1
                        continue
                    clipped_wgs84 = (
                        clipped
                        if dataset.crs.to_epsg() == 4326
                        else shape(transform_geom(dataset.crs, "EPSG:4326", mapping(clipped)))
                    )
                    cell_area, _ = cams_emissions_service.WGS84_GEOD.geometry_area_perimeter(clipped_wgs84)
                    cell_area = abs(float(cell_area))
                    if cell_area <= 0 or not math.isfinite(cell_area):
                        missing_cells += 1
                        continue
                    display_value = raw * multiplier
                    valid_values.append(display_value)
                    area_weighted_sum += display_value * cell_area
                    total_area += cell_area
                    if layer_id == "burnt_carbon":
                        integrated_mass_kg += raw * cell_area * duration_seconds
                    if mode == "grid":
                        cell_wgs84 = (
                            cell
                            if dataset.crs.to_epsg() == 4326
                            else shape(transform_geom(dataset.crs, "EPSG:4326", mapping(cell)))
                        )
                        features.append({
                            "type": "Feature",
                            "geometry": mapping(cell_wgs84),
                            "properties": {"value": display_value, "unit": layer["display_unit"]},
                        })
                        if len(features) > MAX_GRID_FEATURES:
                            raise CamsRequestError(
                                "Grid melebihi 5.000 sel; perbesar peta agar tampilkan area lebih kecil",
                                413,
                                "grid_too_large",
                            )

            summary = {
                "valid_cell_count": len(valid_values),
                "missing_cell_count": missing_cells,
                "maximum": max(valid_values) if valid_values else None,
                "area_weighted_mean": area_weighted_sum / total_area if total_area else None,
                "integrated_mass_kg": integrated_mass_kg if valid_values and layer_id == "burnt_carbon" else None,
                "unit": layer["display_unit"],
                "duration_seconds": duration_seconds if layer_id == "burnt_carbon" else None,
                "area_km2": total_area / 1_000_000,
            }
            if mode == "grid":
                return {
                    "type": "FeatureCollection",
                    "features": features,
                    "summary": summary,
                    "maximum": palette_maximum,
                    "unit": layer["display_unit"],
                }
            return {"summary": summary, "unit": layer["display_unit"]}
    except CamsRequestError:
        raise
    except Exception as exc:
        raise CamsRequestError("Statistik raster CAMS gagal dihitung", 503, "aggregation_failed") from exc


def _geometry_vertex_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    if value and isinstance(value[0], (int, float)):
        return 1
    return sum(_geometry_vertex_count(item) for item in value)


def _frame_duration_seconds(frame: dict[str, Any]) -> float:
    start_text = frame.get("valid_at")
    end_text = frame.get("interval_end")
    if not start_text or not end_text:
        return 86_400.0
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
        duration = (end - start).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return 86_400.0
    return duration if math.isfinite(duration) and duration > 0 else 86_400.0


def _empty_aggregate(layer: dict[str, Any], mode: str) -> dict[str, Any]:
    summary = {
        "valid_cell_count": 0,
        "missing_cell_count": 0,
        "maximum": None,
        "area_weighted_mean": None,
        "integrated_mass_kg": None,
        "unit": layer["display_unit"],
        "duration_seconds": None,
        "area_km2": 0,
    }
    if mode == "grid":
        return {"type": "FeatureCollection", "features": [], "summary": summary, "maximum": layer["maximum"], "unit": layer["display_unit"]}
    return {"summary": summary, "unit": layer["display_unit"]}
