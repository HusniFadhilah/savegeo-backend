"""Dynamic XYZ/PNG tile rendering for admin-ingested local raster imagery
(e.g. BlackSky/BSG very-high-resolution tasking GeoTIFFs that have no GEE
public-catalog equivalent and can't go through `gee_common.get_tile_url()`,
or when GEE asset upload is blocked by billing/GCS bucket access - see
`docs/ntt-earthquake-integration-prompt.md` and
`docs/banjir-sumatera-2025-ingestion-prompt.md`).

Every other tile URL in this codebase comes from Earth Engine's
`image.getMapId()['tile_fetcher'].url_format` - a Leaflet-standard
`.../{z}/{x}/{y}` template. This module produces PNG bytes for exactly that
same tile addressing scheme, on demand, from a local Cloud-Optimized GeoTIFF,
via `rio-tiler` (already declared in pyproject.toml for exactly this - no
Cloud Storage/GEE ingestion, no billed bucket needed). A hand-rolled
`rasterio.warp.reproject`-per-tile version was tried first and works, but
rio-tiler already solves tile-bounds math, nodata/partial-coverage handling,
and per-band percentile rescaling correctly - reuse it instead of
maintaining a parallel implementation.

Source rasters here (verified live against a real BlackSky ortho tif) are
3-band uint16 (12-bit dynamic range packed in 16 bits), no embedded
overviews, UTM projected, `nodata=0`. Callers must run
`ensure_cog(src_path, dst_path)` once at ingestion time (adds internal
tiling + overview pyramids so low-zoom tiles don't require reading the
full-resolution raster) before `render_tile()` is ever called against a
path. `src_path` may be a GDAL virtual-filesystem path (e.g.
`/vsizip/C:/.../some.zip/entry_ortho.tif`) to read straight out of a zip
without extracting it first.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import rasterio
import rasterio.shutil
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from rio_tiler.io import Reader

logger = logging.getLogger(__name__)

TILE_SIZE = 256
_STRETCH_PERCENTILES = [2, 98]


def ensure_cog(src_path: str, dst_path: str, overview_resampling: str = "average") -> None:
    """Rewrite `src_path` as a Cloud-Optimized GeoTIFF at `dst_path` (tiled,
    DEFLATE-compressed, with overview pyramids) if `dst_path` doesn't already
    exist. Idempotent - safe to call every ingestion run. GDAL 3.1+'s COG
    driver (bundled in rasterio) builds overviews itself; no separate
    `gdaladdo` pass needed. `src_path` may be a GDAL vsi path (e.g.
    `/vsizip/...`) to convert straight out of a zip without extracting."""
    if os.path.exists(dst_path):
        return
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with rasterio.open(src_path) as src:
        rasterio.shutil.copy(
            src,
            dst_path,
            driver="COG",
            compress="DEFLATE",
            blocksize=512,
            overview_resampling=overview_resampling,
            bigtiff="IF_SAFER",
        )
    logger.info(f"ensure_cog: wrote COG {dst_path}")


@lru_cache(maxsize=64)
def _stretch_bounds(path: str) -> tuple[tuple[float, float], ...]:
    """Per-band (p2, p98) rescale bounds, computed once per file (rio-tiler
    reads this from the raster's overviews, not full resolution - cheap) and
    cached for the process lifetime. uint16 sources here carry 12-bit
    reflectance-ish values (~0-4095) but the true max depends on the scene,
    so a fixed bit-shift stretch is wrong in general - sample actual data."""
    with Reader(path) as reader:
        stats = reader.statistics(percentiles=_STRETCH_PERCENTILES)
    return tuple((s.percentile_2, s.percentile_98) for s in stats.values())


def render_tile(local_file_path: str, z: int, x: int, y: int) -> bytes | None:
    """Returns PNG bytes for one XYZ tile, or None if the tile doesn't
    overlap the raster's coverage at all (caller should 404)."""
    try:
        with Reader(local_file_path) as reader:
            img = reader.tile(x, y, z, tilesize=TILE_SIZE)
    except (TileOutsideBounds, PointOutsideBounds):
        return None
    except Exception:
        logger.exception(f"render_tile: failed for '{local_file_path}' z={z} x={x} y={y}")
        return None

    in_range = _stretch_bounds(local_file_path)[: img.data.shape[0]]
    img.rescale(in_range=in_range)
    return img.render(img_format="PNG")
