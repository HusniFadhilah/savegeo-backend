"""Auditable, plot-level utilities for the PT Dahana carbon calibration path.

This module deliberately keeps field reference calculations separate from the
national carbon model. It contains no fabricated observations: a calibration
run is ``not_computed`` until plot-level predictions and references exist.
"""

from __future__ import annotations

import csv
import hashlib
import math
import stat
import zipfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

AGB_COEFFICIENT = 3.42
AGB_EXPONENT = 1.15
BGB_COEFFICIENT = 0.207
BGB_EXPONENT = 1.668
CARBON_FRACTION = 0.47
PLOT_AREA_M2 = 400.0
PLOT_AREA_HA = 0.04
CO2_FACTOR = 3.67
SUPPORTED_EXTENSIONS = {
    ".tif",
    ".tiff",
    ".zip",
    ".obj",
    ".csv",
    ".xlsx",
    ".docx",
    ".pdf",
    ".geojson",
    ".json",
    ".shp",
}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024 * 1024


def allometric_agb_kg(dbh_cm: float) -> float:
    if dbh_cm < 0:
        raise ValueError("DBH must be non-negative")
    return AGB_COEFFICIENT * dbh_cm**AGB_EXPONENT


def allometric_bgb_kg(dbh_cm: float) -> float:
    if dbh_cm < 0:
        raise ValueError("DBH must be non-negative")
    return BGB_COEFFICIENT * dbh_cm**BGB_EXPONENT


def tree_carbon_from_dbh(dbh_cm: float) -> dict[str, float]:
    agb = allometric_agb_kg(dbh_cm)
    bgb = allometric_bgb_kg(dbh_cm)
    return {
        "agb_kg": agb,
        "bgb_kg": bgb,
        "carbon_agb_kg": agb * CARBON_FRACTION,
        "carbon_bgb_kg": bgb * CARBON_FRACTION,
    }


def plot_carbon_density(carbon_kg: float, area_ha: float = PLOT_AREA_HA) -> float:
    if area_ha <= 0:
        raise ValueError("Plot area must be positive")
    return (carbon_kg / 1000.0) / area_ha


def reconcile_tree_record(record: dict[str, Any]) -> dict[str, Any]:
    """Recalculate a report row without silently replacing its values."""
    dbh = float(record.get("dbh_cm") or 0)
    expected = tree_carbon_from_dbh(dbh)
    result = {"tree_id": record.get("tree_id"), "expected": expected, "reported": {}}
    for key in expected:
        if record.get(key) is not None:
            reported = float(record[key])
            result["reported"][key] = reported
            result[f"{key}_difference"] = reported - expected[key]
            result[f"{key}_matches"] = math.isclose(reported, expected[key], rel_tol=1e-3, abs_tol=0.01)
    match_values = [value for key, value in result.items() if key.endswith("_matches")]
    result["status"] = "match" if match_values and all(match_values) else "review"
    return result


def _segments_intersect(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
) -> bool:
    def orient(p, q, r):
        value = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return (value > 1e-9) - (value < -1e-9)

    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def build_plot_polygon(
    plot_id: str, corners: Iterable[Iterable[float]], crs: str = "EPSG:32748"
) -> dict[str, Any]:
    points = [(float(item[0]), float(item[1])) for item in corners]
    if len(points) != 4 or len({point for point in points}) != 4:
        raise ValueError("A plot requires four unique corner coordinates")
    for i in range(4):
        for j in range(i + 1, 4):
            if j in {i, (i + 1) % 4} or (i == 0 and j == 3):
                continue
            if _segments_intersect(points[i], points[(i + 1) % 4], points[j], points[(j + 1) % 4]):
                raise ValueError("Plot polygon self-intersects; preserve source order for manual review")
    area = (
        abs(
            sum(
                points[i][0] * points[(i + 1) % 4][1] - points[(i + 1) % 4][0] * points[i][1]
                for i in range(4)
            )
        )
        / 2
    )
    if area <= 0:
        raise ValueError("Plot polygon has zero area")
    closed = points + [points[0]]
    feature = {
        "type": "Feature",
        "properties": {"plot_id": plot_id, "crs": crs, "area_m2": area},
        "geometry": {"type": "Polygon", "coordinates": [[list(point) for point in closed]]},
    }
    return {
        "original": feature,
        "normalized": feature,
        "area_m2": area,
        "area_ok": abs(area - PLOT_AREA_M2) <= max(10, PLOT_AREA_M2 * 0.1),
        "crs": crs,
    }


def geojson_feature_collection(plots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": "dahana_subang_2026_plots",
        "crs": {"type": "name", "properties": {"name": "EPSG:32748"}},
        "features": plots,
    }


def evaluate_predictions(rows: list[dict[str, float]]) -> dict[str, Any]:
    if len(rows) < 2:
        return {"status": "not_computed", "reason": "At least two independent plots are required"}
    observed = np.asarray([row["observed"] for row in rows], dtype=float)
    predicted = np.asarray([row["predicted"] for row in rows], dtype=float)
    residual = predicted - observed
    ss_total = float(np.sum((observed - observed.mean()) ** 2))
    return {
        "status": "computed",
        "n_plots": len(rows),
        "bias": float(residual.mean()),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mae": float(np.mean(np.abs(residual))),
        "r2": None if ss_total == 0 else float(1 - np.sum(residual**2) / ss_total),
        "rows": [
            {**row, "residual": float(err), "absolute_error": float(abs(err))}
            for row, err in zip(rows, residual)
        ],
    }


def lopo_linear_calibration(rows: list[dict[str, float]]) -> dict[str, Any]:
    if len(rows) < 3:
        return {
            "status": "not_computed",
            "reason": "LOPO linear calibration requires at least three independent plots",
        }
    results = []
    for index, held_out in enumerate(rows):
        train = [row for offset, row in enumerate(rows) if offset != index]
        x = np.asarray([row["predicted"] for row in train], dtype=float)
        y = np.asarray([row["observed"] for row in train], dtype=float)
        if np.ptp(x) == 0:
            intercept, slope = float(y.mean()), 0.0
        else:
            slope, intercept = np.polyfit(x, y, 1)
        prediction = float(intercept + slope * held_out["predicted"])
        results.append(
            {
                "plot_id": held_out.get("plot_id"),
                "observed": held_out["observed"],
                "predicted": prediction,
                "intercept": float(intercept),
                "slope": float(slope),
            }
        )
    metrics = evaluate_predictions(results)
    metrics["validation_method"] = "leave_one_plot_out"
    metrics["calibration_intercept"] = float(np.mean([item["intercept"] for item in results]))
    metrics["calibration_slope"] = float(np.mean([item["slope"] for item in results]))
    return metrics


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def file_kind(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".tif": "raster",
        ".tiff": "raster",
        ".zip": "archive",
        ".obj": "point_cloud",
        ".csv": "table",
        ".xlsx": "table",
        ".geojson": "vector",
        ".json": "vector",
        ".shp": "vector",
        ".docx": "report",
        ".pdf": "report",
    }.get(suffix, "unknown")


def safe_extract_zip(archive: Path, destination: Path, max_bytes: int = MAX_ZIP_EXPANDED_BYTES) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    total = 0
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            name = member.filename.replace("\\", "/")
            target = (destination / name).resolve()
            if (
                name.startswith("/")
                or ".." in Path(name).parts
                or target != destination.resolve()
                and destination.resolve() not in target.parents
            ):
                raise ValueError(f"ZIP Slip path rejected: {member.filename}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (mode and not stat.S_ISREG(mode) and not member.is_dir()):
                raise ValueError(f"Symlink or special ZIP member rejected: {member.filename}")
            if Path(name).suffix.lower() in {".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh"}:
                raise ValueError(f"Executable ZIP member rejected: {member.filename}")
            total += member.file_size
            if total > max_bytes:
                raise ValueError("ZIP expanded size exceeds safety limit")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            extracted.append(str(target.relative_to(destination)))
    return extracted


def inspect_raster(path: Path) -> dict[str, Any]:
    try:
        import rasterio
    except ImportError:
        return {"status": "not_computed", "reason": "rasterio is not installed"}
    with rasterio.open(path) as raster:
        return {
            "status": "computed",
            "product_type": "unconfirmed",
            "vertical_unit": "unconfirmed",
            "driver": raster.driver,
            "crs": str(raster.crs) if raster.crs else None,
            "width": raster.width,
            "height": raster.height,
            "count": raster.count,
            "dtype": raster.dtypes,
            "nodata": raster.nodata,
            "transform": tuple(raster.transform),
            "resolution": raster.res,
            "bounds": tuple(raster.bounds),
            "compression": str(raster.compression) if raster.compression else None,
            "masked": bool(raster.mask_flag_enums),
            "checksum": [raster.checksum(index) for index in range(1, raster.count + 1)],
        }


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))
