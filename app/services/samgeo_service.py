"""Bounded SamGeo jobs; raster reads stay in the API environment, ML is isolated."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

import numpy as np
import rasterio
from rasterio.features import geometry_mask, shapes
from rasterio.transform import rowcol
from rasterio.warp import transform, transform_geom
from rio_tiler.io import Reader

from app.services.gee_common import AnalysisError
from app.services import imagery_service as imagery

ROOT = Path(__file__).resolve().parents[2]
JOB_ROOT = ROOT / "var" / "samgeo"
logger = logging.getLogger(__name__)


def _python() -> str:
    configured = os.getenv("SAMGEO_PYTHON")
    if configured:
        return configured
    from importlib.util import find_spec
    if find_spec("samgeo"):
        return sys.executable
    for name in (".venv-samgeo", ".venv-py312-test"):
        candidate = ROOT / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def capabilities() -> dict:
    try:
        result = subprocess.run(
            [_python(), "-c", "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('samgeo') else 1)"],
            capture_output=True, timeout=15,
        )
        ready = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    return {"available": ready, "model": "SAM ViT-B", "max_size": 1024,
            "message": "" if ready else "SamGeo belum tersedia pada server. Pasang extra samgeo atau atur SAMGEO_PYTHON."}


def _write_json(path: Path, value: dict) -> None:
    if path.exists() and "owner" not in value:
        previous_owner = json.loads(path.read_text(encoding="utf-8")).get("owner")
        if previous_owner:
            value = {**value, "owner": previous_owner}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def get_job(job_id: str, owner: str | None = None) -> dict:
    try:
        job_id = str(UUID(job_id))
    except ValueError as exc:
        raise AnalysisError("ID segmentasi tidak valid", 400) from exc
    path = JOB_ROOT / job_id / "status.json"
    if not path.exists():
        raise AnalysisError("Hasil segmentasi tidak ditemukan atau sudah kedaluwarsa", 404)
    status = json.loads(path.read_text(encoding="utf-8"))
    if owner is not None and status.get("owner") != owner:
        raise AnalysisError("Hasil segmentasi tidak ditemukan atau sudah kedaluwarsa", 404)
    status.pop("owner", None)
    if status["status"] == "running" and time.time() - path.stat().st_mtime > 1800:
        return {"job_id": job_id, "status": "failed", "message": "Pekerjaan terhenti atau melewati batas waktu server."}
    return status


def start_job(data: dict, owner: str = "disaster-viewer") -> dict:
    if not data.get("item_url") or not data.get("aoi"):
        raise AnalysisError("Scene COG dan AOI diperlukan untuk segmentasi", 400)
    provider_key = str(data.get("provider_key") or "")
    if provider_key in imagery.COMMERCIAL_PROVIDER_KEYS:
        imagery._readable_stac_asset_href(data["item_url"], data.get("asset_key") or "visual", provider_key)
    else:
        imagery._validate_public_http_url(data["item_url"], "URL scene")
    imagery._aoi_payload_to_bbox(data["aoi"])
    imagery._parse_cog_bands(data.get("bands"))
    imagery._parse_cog_rescale(data.get("rescale"), 3)
    capability = capabilities()
    if not capability["available"]:
        raise AnalysisError(capability["message"], 503)
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    lock = JOB_ROOT / "running.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime > 1800:
        lock.unlink(missing_ok=True)
    try:
        # A filesystem lock also limits concurrent inference across API workers.
        with lock.open("x") as stream:
            stream.write(str(time.time()))
    except FileExistsError as exc:
        raise AnalysisError("SamGeo sedang memproses scene lain. Coba lagi setelah selesai.", 409) from exc
    job_id = str(uuid4())
    folder = JOB_ROOT / job_id
    try:
        folder.mkdir()
        status = {"job_id": job_id, "status": "running", "message": "Membaca COG dan menyiapkan model SAM ViT-B", "owner": owner}
        _write_json(folder / "status.json", status)
        threading.Thread(target=_run_job, args=(folder, data), daemon=True).start()
    except Exception:
        lock.unlink(missing_ok=True)
        raise
    return {key: value for key, value in status.items() if key != "owner"}


def _run_job(folder: Path, data: dict) -> None:
    try:
        result = segment(data, folder)
        _write_json(folder / "status.json", {"job_id": folder.name, "status": "complete", "result": result})
    except Exception:
        logger.exception("SamGeo job failed")
        _write_json(folder / "status.json", {"job_id": folder.name, "status": "failed", "message": "Segmentasi gagal. Coba lagi atau hubungi administrator."})
    finally:
        for filename in ("input.tif", "mask.tif"):
            (folder / filename).unlink(missing_ok=True)
        (JOB_ROOT / "running.lock").unlink(missing_ok=True)


def segment(data: dict, folder: Path) -> dict:
    href = imagery._readable_stac_asset_href(
        data["item_url"], data.get("asset_key") or "visual", data.get("provider_key") or None
    )
    bounds = imagery._aoi_payload_to_bbox(data["aoi"])
    indexes = imagery._parse_cog_bands(data.get("bands"))
    with rasterio.Env(GDAL_HTTP_TIMEOUT=60, GDAL_HTTP_MAX_RETRY=2):
        with Reader(href) as reader:
            if indexes is None:
                indexes = list(range(1, min(reader.dataset.count, 3) + 1))
            image = reader.part(bounds, bounds_crs="EPSG:4326", dst_crs="EPSG:3857",
                                max_size=1024, indexes=indexes)
    if image.data.shape[0] not in (1, 3):
        raise AnalysisError("Segmentasi memerlukan satu band atau tiga band RGB", 400)
    pixels = image.data
    valid = image.mask > 0
    aoi = data["aoi"]
    geometry = aoi.get("geometry", aoi)
    projected = transform_geom("EPSG:4326", image.crs, geometry)
    valid &= geometry_mask([projected], out_shape=valid.shape, transform=image.transform, invert=True)
    if not valid.any():
        raise AnalysisError("Tidak ada piksel scene yang valid di dalam AOI", 400)
    ranges = imagery._parse_cog_rescale(data.get("rescale"), pixels.shape[0])
    rgb = np.zeros(pixels.shape, dtype="uint8")
    for index, band in enumerate(pixels):
        low, high = ranges[index] if ranges else ((0, 255) if pixels.dtype == np.uint8 else np.percentile(band[valid], [2, 98]))
        rgb[index] = np.clip((band.astype("float32") - low) * 255 / max(float(high - low), 1), 0, 255).astype("uint8")
    rgb[:, ~valid] = 0
    if rgb.shape[0] == 1:
        rgb = np.repeat(rgb, 3, axis=0)
    input_path = folder / "input.tif"
    output_path = folder / "mask.tif"
    with rasterio.open(input_path, "w", driver="GTiff", height=rgb.shape[1], width=rgb.shape[2],
                       count=3, dtype="uint8", crs=image.crs, transform=image.transform) as dataset:
        dataset.write(rgb)
    try:
        completed = subprocess.run(
            [_python(), str(Path(__file__).with_name("samgeo_worker.py")), str(input_path), str(output_path),
             str(JOB_ROOT / "checkpoints")], capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("SamGeo melewati batas 15 menit. Coba AOI yang lebih kecil.", 504) from exc
    if completed.returncode:
        logger.error("SamGeo worker: %s", completed.stderr[-4000:])
        raise AnalysisError("SamGeo gagal menjalankan model. Periksa instalasi, unduhan checkpoint, dan log backend.", 502)
    with rasterio.open(output_path) as dataset:
        labels = dataset.read(1).astype("int32")
    labels[~valid] = 0
    automatic_labels = np.unique(labels[labels > 0])
    selected_mask = labels > 0
    seed_points = data.get("seed_points") or []
    selected_labels = automatic_labels
    if seed_points:
        valid_points = [
            point for point in seed_points
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        selected_ids: set[int] = set()
        if valid_points:
            xs, ys = transform(
                "EPSG:4326",
                image.crs,
                [float(point[0]) for point in valid_points],
                [float(point[1]) for point in valid_points],
            )
            radius = max(1, min(int(data.get("seed_radius_px", 12)), 64))
            for x, y in zip(xs, ys):
                row, col = rowcol(image.transform, x, y)
                row_min, row_max = max(0, row - radius), min(labels.shape[0], row + radius + 1)
                col_min, col_max = max(0, col - radius), min(labels.shape[1], col + radius + 1)
                selected_ids.update(int(value) for value in np.unique(labels[row_min:row_max, col_min:col_max]) if value > 0)
        selected_labels = np.asarray(sorted(selected_ids), dtype="int32")
        selected_mask = np.isin(labels, selected_labels) if selected_ids else np.zeros(labels.shape, dtype=bool)

    features = []
    for geometry, label in shapes(labels, mask=selected_mask, transform=image.transform):
        features.append({"type": "Feature", "geometry": transform_geom(image.crs, "EPSG:4326", geometry),
                         "properties": {"segment_id": int(label), "model": "SAM ViT-B",
                                        "seeded_by": "MODIS FireMask" if seed_points else "automatic"}})
    return {"type": "FeatureCollection", "features": features,
            "metadata": {"scene": data["item_url"], "asset": data.get("asset_key"), "width": rgb.shape[2],
                         "height": rgb.shape[1], "model": "SAM ViT-B",
                         "automatic_object_count": int(automatic_labels.size),
                         "object_count": int(selected_labels.size),
                         "seed_count": len(seed_points),
                         "selection_method": "MODIS-seeded SAM" if seed_points else "automatic SAM"}}
