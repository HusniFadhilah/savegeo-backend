"""Safe EGM2008 vertical metadata and scalar conversions.

This module intentionally does not invent geoid undulation values. A scalar
conversion is allowed only when the caller supplies a known geoid value from a
verified EGM2008 grid/control point. Raster conversion remains blocked until a
verified grid is installed and configured.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


EGM2008_VERTICAL_CRS = "EPSG:3855"
VERTICAL_STATUSES = {"known", "converted", "assumed", "unknown", "incompatible", "conversion_failed"}


def _configured_grid() -> Path | None:
    raw = os.getenv("SAVEGEO_EGM2008_GRID", "").strip()
    path = Path(raw) if raw else None
    return path if path and path.is_file() else None


def egm2008_status() -> dict[str, Any]:
    grid = _configured_grid()
    checksum = None
    if grid is not None:
        digest = hashlib.sha256()
        with grid.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = f"sha256:{digest.hexdigest()}"
    return {
        "model": "EGM2008",
        "available": grid is not None,
        "version": os.getenv("SAVEGEO_EGM2008_GRID_VERSION") if grid else None,
        "grid": grid.name if grid else None,
        "checksum": checksum,
        "coverage": "global" if grid else None,
        "last_validation": os.getenv("SAVEGEO_EGM2008_LAST_VALIDATION") if grid else None,
        "library": "PROJ/GDAL compatible grid; scalar formula in SaveGeo",
        "vertical_crs": EGM2008_VERTICAL_CRS,
        "message": None if grid else "Verified EGM2008 grid is not configured; conversion is unavailable.",
    }


def convert_ellipsoidal_to_orthometric(
    ellipsoidal_height_m: float,
    geoid_undulation_m: float,
    *,
    source_vertical_reference: str,
    geoid_model: str = "EGM2008",
) -> dict[str, Any]:
    source = source_vertical_reference.strip().upper()
    model = geoid_model.strip().upper()
    if source in {"UNKNOWN", "", "UNSPECIFIED"}:
        raise ValueError("source vertical reference is unknown")
    if model != "EGM2008":
        raise ValueError("only EGM2008 is supported; EGM96 or an unspecified geoid is rejected")
    if source not in {"ELLIPSOIDAL", "WGS84_ELLIPSOID", "WGS 84 ELLIPSOID"}:
        raise ValueError("input must be an ellipsoidal height")
    orthometric = float(ellipsoidal_height_m) - float(geoid_undulation_m)
    return {
        "source_height_m": float(ellipsoidal_height_m),
        "source_height_type": "ellipsoidal",
        "source_vertical_reference": source_vertical_reference,
        "orthometric_height_m": orthometric,
        "height_type": "orthometric",
        "height_unit": "metre",
        "vertical_reference": "EGM2008",
        "vertical_crs": EGM2008_VERTICAL_CRS,
        "geoid_undulation_m": float(geoid_undulation_m),
        "transformation_method": "H = h - N",
        "transformation_pipeline": "verified EGM2008 geoid undulation supplied by caller",
        "geoid_model": "EGM2008",
        "vertical_reference_status": "converted",
    }
