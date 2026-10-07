"""Serve validated, locally archived CAMS fields; HTTP reads never start ADS jobs."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import COGReader

from app.core.config import get_settings
from app.services import cams_emissions_service
from app.services.cams_emissions_service import CamsRequestError

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
