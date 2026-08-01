"""
local_raster_provider.py — rasterio-based, non-GEE raster ingestion.

Provides the shared grid/warp primitive (`GridSpec`, `load_raster_to_grid`)
used both for local/already-downloaded GeoTIFFs and for STAC-signed HTTPS
COG hrefs (providers/stac_provider.py reuses `load_raster_to_grid` — a
signed STAC asset href is just an HTTPS URL rasterio can open the same way
as a local path).

Also implements `load_carbon_reference_to_grid()`: non-GEE label ingestion
for the tiled-COG carbon reference datasets in CARBON_EXTERNAL_REGISTRY
(HANSEN_TREECOVER_AGB_PROXY, ESA_CCI_BIOMASS_COG) — mosaics whichever 10x10
degree tiles intersect the target grid, applies nodata/lossyear masking and
the registry's unit transform.

Requires: rasterio (already installed, 1.4.4). No new dependency.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from app.providers.feature_engineering_non_gee import NonGeeStack

logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]  # (west, south, east, north) in EPSG:4326

# GDAL/vsicurl tuning for remote COG reads (Planetary Computer / CEDA HTTPS).
# Without these, every rasterio.open() on a remote COG re-negotiates a fresh
# connection and re-reads the TIFF directory with no caching — observed to
# take 10-30s per band on this network. This is the standard cloud-native
# GeoTIFF tuning recommended by Planetary Computer's own docs.
GDAL_HTTP_OPTS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": 200_000_000,
    "GDAL_CACHEMAX": 200,
    "GDAL_HTTP_MAX_RETRY": 3,
    "GDAL_HTTP_RETRY_DELAY": 1,
}


class GridSpec:
    """Common target grid shared by every provider so S2/DEM/WorldCover/label
    rasters all land on identical pixel grids without separate resampling
    passes downstream.
    """

    def __init__(self, bbox_lonlat: BBox, resolution_m: float, dst_crs: Optional[str] = None):
        import rasterio
        from rasterio.crs import CRS
        from rasterio.warp import calculate_default_transform

        self.bbox_lonlat = bbox_lonlat
        self.resolution_m = resolution_m
        west, south, east, north = bbox_lonlat

        if dst_crs is None:
            center_lon = (west + east) / 2
            utm_zone = int(math.floor((center_lon + 180) / 6) % 60) + 1
            hemisphere = 326 if (south + north) / 2 >= 0 else 327
            dst_crs = f"EPSG:{hemisphere}{utm_zone:02d}"
        self.crs = CRS.from_user_input(dst_crs)

        src_crs = CRS.from_epsg(4326)
        transform, width, height = calculate_default_transform(
            src_crs, self.crs, 2, 2, west, south, east, north,
            resolution=(resolution_m, resolution_m),
        )
        self.transform = transform
        self.width = int(width)
        self.height = int(height)
        self.shape = (self.height, self.width)

    def to_affine_and_shape(self):
        return self.transform, self.width, self.height, self.crs


def load_raster_to_grid(
    path_or_url: str,
    grid: GridSpec,
    band_index: int = 1,
    resampling: str = "bilinear",
    src_nodata: Optional[float] = None,
) -> Optional[np.ndarray]:
    """Read one band of a raster (local path or plain HTTPS/GCS COG URL) and
    reproject/resample it onto `grid` via rasterio WarpedVRT.

    Returns an (H, W) float array with NaN outside the source's valid/covered
    area, or None if the source cannot be opened (network failure, 404, not
    found) — callers must treat None as "this tile unavailable," not crash,
    matching the ocean-tile-missing behavior already used for Hansen tiles.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    resampling_enum = getattr(Resampling, resampling)
    env_opts = GDAL_HTTP_OPTS if str(path_or_url).startswith("http") else {}
    try:
        with rasterio.Env(**env_opts), rasterio.open(path_or_url) as src:
            nodata = src.nodata if src_nodata is None else src_nodata
            with WarpedVRT(
                src,
                crs=grid.crs,
                transform=grid.transform,
                width=grid.width,
                height=grid.height,
                resampling=resampling_enum,
                src_nodata=nodata,
                nodata=np.nan,
            ) as vrt:
                data = vrt.read(band_index).astype(np.float32)
            return data
    except Exception as exc:
        logger.warning(f"load_raster_to_grid: could not read '{path_or_url}': {exc}")
        return None


def build_stack_from_local_files(file_map: Dict[str, str], grid: GridSpec) -> NonGeeStack:
    """file_map e.g. {'B2': 'path/b2.tif', ..., 'elevation': 'dem.tif'}.

    Used for (a) local test fixtures when validating grid-alignment logic
    without hitting Planetary Computer, and (b) any locally-cached composite
    reused across repeated experiments.
    """
    bands: Dict[str, np.ndarray] = {}
    for name, path in file_map.items():
        arr = load_raster_to_grid(path, grid)
        if arr is None:
            raise ValueError(f"build_stack_from_local_files: failed to load required band '{name}' from '{path}'")
        bands[name] = arr

    return NonGeeStack(
        bands=bands,
        transform=grid.transform,
        crs=grid.crs,
        shape=grid.shape,
        provenance={"provider": "local_raster", "file_map": file_map},
    )


# ─────────────────────────────────────────────
# Tiled-COG carbon reference mosaicking (non-GEE labels)
# ─────────────────────────────────────────────

def _enumerate_tile_origins(bbox: BBox, tile_size_deg: int = 10) -> List[Tuple[int, int]]:
    """South-west-corner tile origins (floor convention) intersecting bbox."""
    west, south, east, north = bbox
    lat_start = int(math.floor(south / tile_size_deg)) * tile_size_deg
    lon_start = int(math.floor(west / tile_size_deg)) * tile_size_deg
    lats = range(lat_start, int(math.ceil(north / tile_size_deg)) * tile_size_deg, tile_size_deg)
    lons = range(lon_start, int(math.ceil(east / tile_size_deg)) * tile_size_deg, tile_size_deg)
    return [(la, lo) for la in lats for lo in lons]


def _hansen_tile_url(lat_origin: int, lon_origin: int, template: str, tile_size_deg: int = 10) -> str:
    from providers.external_carbon_provider import _tile_key
    lat_str, lon_str = _tile_key(lat_origin + tile_size_deg / 2, lon_origin + tile_size_deg / 2, tile_size_deg)
    return template.format(lat_tile=lat_str, lon_tile=lon_str)


def _ceda_tile_url(lat_origin: int, lon_origin: int, template: str, year: int) -> str:
    ns = "N" if lat_origin >= 0 else "S"
    ew = "E" if lon_origin >= 0 else "W"
    tile_id = f"{ns}{abs(lat_origin):02d}{ew}{abs(lon_origin):03d}"
    return template.format(tile_id=tile_id, year=year)


def _apply_transform_array(arr: np.ndarray, transform: str) -> np.ndarray:
    """Array-safe version of external_carbon_provider._apply_transform
    (that one is scalar-only: `if value is None` breaks on ndarrays)."""
    if transform == "divide_10":
        return arr / 10.0
    if transform == "multiply_0.47":
        return arr * 0.47
    if transform == "multiply_0.94":
        return arr * 0.94
    if transform == "multiply_2.0":
        return arr * 2.0
    if transform in ("none", "", None):
        return arr
    logger.warning(f"Unknown transform '{transform}', returning raw array")
    return arr


def _mosaic_tiled_cog(
    url_builder: Callable[[int, int], str],
    grid: GridSpec,
    nodata: Optional[float] = None,
    resampling: str = "bilinear",
    lossyear_url_builder: Optional[Callable[[int, int], str]] = None,
) -> Tuple[np.ndarray, int]:
    """Mosaic whichever tiles intersect grid's bbox onto grid; first-valid wins.

    Returns (mosaic array with NaN where no tile covered a pixel, n_tiles_used).
    """
    accumulator = np.full(grid.shape, np.nan, dtype=np.float32)
    n_tiles_used = 0

    for lat_o, lon_o in _enumerate_tile_origins(grid.bbox_lonlat):
        url = url_builder(lat_o, lon_o)
        tile_data = load_raster_to_grid(url, grid, resampling=resampling, src_nodata=nodata)
        if tile_data is None:
            continue

        if lossyear_url_builder is not None:
            loss_url = lossyear_url_builder(lat_o, lon_o)
            loss_data = load_raster_to_grid(loss_url, grid, resampling="nearest")
            if loss_data is not None:
                deforested = loss_data > 0
                tile_data = np.where(deforested, np.nan, tile_data)

        valid_here = ~np.isnan(tile_data)
        need_fill = np.isnan(accumulator) & valid_here
        accumulator = np.where(need_fill, tile_data, accumulator)
        n_tiles_used += 1

    return accumulator, n_tiles_used


_CEDA_AVAILABLE_YEARS = [2010, 2017, 2018, 2019, 2020]


def load_carbon_reference_to_grid(
    dataset_key: str, grid: GridSpec, year: Optional[int] = None,
) -> Tuple[np.ndarray, Dict]:
    """Load a non-GEE-sampleable carbon reference dataset from
    CARBON_EXTERNAL_REGISTRY onto `grid`, applying the registry's unit
    transform. Returns (label_array[H,W] in Mg C/ha with NaN outside
    coverage, info dict with n_tiles_used/transform/unit for provenance).

    Supported today: HANSEN_TREECOVER_AGB_PROXY, ESA_CCI_BIOMASS_COG.
    """
    from carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY

    meta = CARBON_EXTERNAL_REGISTRY.get(dataset_key)
    if meta is None:
        raise ValueError(f"'{dataset_key}' not found in CARBON_EXTERNAL_REGISTRY")
    if not meta.get("is_tiled"):
        raise NotImplementedError(
            f"load_carbon_reference_to_grid only supports tiled COG datasets currently; "
            f"'{dataset_key}' has is_tiled={meta.get('is_tiled')}"
        )

    if dataset_key == "HANSEN_TREECOVER_AGB_PROXY":
        url_builder = lambda la, lo: _hansen_tile_url(la, lo, meta["tile_url_template"], meta.get("tile_size_deg", 10))
        loss_builder = None
        if meta.get("mask_lossyear") and meta.get("lossyear_url_template"):
            loss_builder = lambda la, lo: _hansen_tile_url(
                la, lo, meta["lossyear_url_template"], meta.get("tile_size_deg", 10)
            )
        mosaic, n_tiles = _mosaic_tiled_cog(
            url_builder, grid, nodata=meta.get("nodata"), resampling="bilinear",
            lossyear_url_builder=loss_builder,
        )
    elif dataset_key == "ESA_CCI_BIOMASS_COG":
        target_year = year or meta.get("year", 2020)
        nearest_year = min(_CEDA_AVAILABLE_YEARS, key=lambda y: abs(y - target_year))
        if nearest_year != target_year:
            logger.info(f"ESA_CCI_BIOMASS_COG: year {target_year} unavailable, using nearest available {nearest_year}")
        url_builder = lambda la, lo: _ceda_tile_url(la, lo, meta["tile_url_template"], nearest_year)
        mosaic, n_tiles = _mosaic_tiled_cog(url_builder, grid, nodata=meta.get("nodata"), resampling="bilinear")
    else:
        raise NotImplementedError(f"No tiled-COG mosaicking logic implemented for '{dataset_key}' yet")

    label = _apply_transform_array(mosaic, meta.get("transform", "none"))

    if n_tiles == 0:
        raise ValueError(
            f"'{dataset_key}': 0 tiles intersected the requested grid bbox {grid.bbox_lonlat} "
            "— check bbox is within dataset coverage."
        )

    return label, {
        "dataset_key": dataset_key,
        "n_tiles_used": n_tiles,
        "transform": meta.get("transform", "none"),
        "unit": meta.get("unit"),
        "target_pool": meta.get("target_pool"),
        "label_year": (nearest_year if dataset_key == "ESA_CCI_BIOMASS_COG" else meta.get("year")),
    }


if __name__ == "__main__":
    # Smoke test 1: GridSpec construction over a tiny bbox, sane shape/CRS.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    bbox = (110.3, -7.6, 110.6, -7.3)  # tiny Central Java bbox
    grid = GridSpec(bbox, resolution_m=100)
    print(f"GridSpec: shape={grid.shape}, crs={grid.crs}, transform={grid.transform}")
    assert grid.width > 0 and grid.height > 0
    assert str(grid.crs).startswith("EPSG:327")  # southern-hemisphere UTM
    print("GridSpec construction OK")

    # Smoke test 2: synthetic local GeoTIFF round-trip through load_raster_to_grid.
    import rasterio
    from rasterio.transform import from_bounds
    import tempfile, os

    tmp_dir = tempfile.mkdtemp()
    src_path = os.path.join(tmp_dir, "synthetic_dem.tif")
    src_shape = (100, 100)
    src_transform = from_bounds(*bbox, src_shape[1], src_shape[0])
    synthetic = np.linspace(0, 1000, src_shape[0] * src_shape[1]).reshape(src_shape).astype(np.float32)
    with rasterio.open(
        src_path, "w", driver="GTiff", height=src_shape[0], width=src_shape[1],
        count=1, dtype="float32", crs="EPSG:4326", transform=src_transform, nodata=-9999,
    ) as dst:
        dst.write(synthetic, 1)

    warped = load_raster_to_grid(src_path, grid)
    assert warped is not None and warped.shape == grid.shape
    assert not np.all(np.isnan(warped)), "warped output should have valid data over the same bbox"
    print(f"load_raster_to_grid round-trip OK: output shape {warped.shape}, mean={np.nanmean(warped):.2f}")

    stack = build_stack_from_local_files({"elevation": src_path}, grid)
    assert "elevation" in stack.bands and stack.bands["elevation"].shape == grid.shape
    print("build_stack_from_local_files OK")

    missing = load_raster_to_grid("https://example.invalid/does-not-exist.tif", grid)
    assert missing is None, "nonexistent raster should return None, not raise"
    print("Missing-tile graceful-None handling OK")

    print("All local_raster_provider.py smoke tests passed.")
