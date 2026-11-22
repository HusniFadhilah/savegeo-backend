"""
local_raster_provider.py — rasterio-based, non-GEE raster ingestion.

Provides the shared grid/warp primitive (`GridSpec`, `load_raster_to_grid`)
used both for local/already-downloaded GeoTIFFs and STAC-signed HTTPS COGs.
Remote COGs are read by byte range through a short-lived loopback proxy backed
by a bounded, DNS-pinned HTTPS transport.

Also implements `load_carbon_reference_to_grid()`: non-GEE label ingestion
for the tiled/global-COG carbon reference datasets in CARBON_EXTERNAL_REGISTRY
(HANSEN_TREECOVER_AGB_PROXY, ESA_CCI_BIOMASS_COG, ESA_CCI_BIOMASS_V7_COG,
CTREES_AGB_100M) — reads only the AOI window, applies nodata/lossyear masking,
and applies the registry's unit transform.

Requires: rasterio (already installed, 1.4.4). No new dependency.
"""
from __future__ import annotations

import logging
import math
import hashlib
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Callable, Iterator
from pathlib import Path
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import numpy as np

from app.core.public_http import public_http
from app.providers.feature_engineering_non_gee import NonGeeStack

logger = logging.getLogger(__name__)
_CARBON_TILE_CACHE_LOCK = threading.RLock()
_CARBON_TILE_STATS = {
    "requests": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "rendered": 0,
    "empty": 0,
    "errors": 0,
    "source_reads": 0,
    "source_limit_skips": 0,
    "total_render_ms": 0.0,
}


def get_carbon_tile_metrics() -> dict:
    """Return process-local COG tile/cache counters for operations monitoring."""
    from app.core.config import get_settings

    with _CARBON_TILE_CACHE_LOCK:
        stats = dict(_CARBON_TILE_STATS)
    rendered = int(stats["rendered"])
    stats["average_render_ms"] = round(stats["total_render_ms"] / rendered, 1) if rendered else 0.0
    stats["cache_hit_rate_pct"] = round(stats["cache_hits"] / stats["requests"] * 100, 1) if stats["requests"] else 0.0
    settings = get_settings()
    configured = str(getattr(settings, "carbon_tile_cache_dir", "") or "").strip()
    return {
        **stats,
        "cache_dir": str(
            Path(configured)
            if configured
            else Path(settings.upload_dir).resolve().parent / "carbon_tile_cache"
        ),
    }


def _tile_stat(name: str, value: int | float = 1) -> None:
    with _CARBON_TILE_CACHE_LOCK:
        _CARBON_TILE_STATS[name] = _CARBON_TILE_STATS.get(name, 0) + value


def record_carbon_tile_error() -> None:
    """Record a route-level tile failure without exposing internals."""
    _tile_stat("errors")


def _carbon_tile_cache_path(cache_key: str) -> Path:
    """Return the persistent cache path for one rendered tile."""
    from app.core.config import get_settings

    settings = get_settings()
    configured = str(getattr(settings, "carbon_tile_cache_dir", "") or "").strip()
    root = Path(configured) if configured else Path(settings.upload_dir).resolve().parent / "carbon_tile_cache"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return root / digest[:2] / f"{digest}.png"


def _read_cached_carbon_tile(cache_key: str) -> bytes | None:
    from app.core.config import get_settings

    path = _carbon_tile_cache_path(cache_key)
    ttl = max(60, int(getattr(get_settings(), "carbon_tile_cache_ttl_seconds", 86_400)))
    try:
        if time.time() - path.stat().st_mtime <= ttl:
            return path.read_bytes()
    except OSError:
        return None
    return None


def _write_cached_carbon_tile(cache_key: str, content: bytes) -> None:
    path = _carbon_tile_cache_path(cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
    except OSError as exc:
        logger.debug("Could not persist carbon tile cache %s: %s", path, exc)

BBox = tuple[float, float, float, float]  # (west, south, east, north) in EPSG:4326

MAX_GRID_PIXELS = 50_000_000
MAX_REMOTE_RASTER_OBJECT_BYTES = 128 * 1024 * 1024 * 1024
MAX_REMOTE_RASTER_RANGE_BYTES = 16 * 1024 * 1024
MAX_REMOTE_RASTER_SESSION_BYTES = 512 * 1024 * 1024


def validate_remote_raster_url(path_or_url: str, http_options: dict[str, str] | None = None) -> str | None:
    """Validate remote raster syntax and reject non-public DNS answers.

    Rasterio must never receive an external URL. Use ``safe_raster_source``
    to read through its loopback proxy and the bounded HTTPS transport.
    """
    source = str(path_or_url).strip()
    if re.match(r"^[A-Za-z]:[\\/]", source):
        return None
    parsed = urlsplit(source)
    if not parsed.scheme:
        if source.lower().startswith(("/vsi", "\\\\")):
            raise ValueError("Remote GDAL virtual paths and network shares are not allowed.")
        return None
    if parsed.scheme.lower() != "https":
        raise ValueError("Remote raster must use public HTTPS.")
    try:
        _, hostname, _ = public_http.validate_public_destination(source)
    except (ValueError, OSError) as exc:
        raise ValueError("Remote raster must use public HTTPS.") from exc
    return hostname


def _raster_http_headers(http_options: dict[str, str] | None) -> dict[str, str]:
    """Extract the one credential header allowed for remote raster downloads."""
    configured = str((http_options or {}).get("GDAL_HTTP_HEADERS") or "")
    headers: dict[str, str] = {}
    for line in configured.splitlines():
        name, separator, value = line.partition(":")
        if not separator or name.strip().lower() != "authorization" or not value.strip():
            raise ValueError("Remote raster provider supplied an unsupported HTTP header.")
        if headers:
            raise ValueError("Remote raster provider supplied duplicate authorization headers.")
        headers["Authorization"] = value.strip()
    return headers


@contextmanager
def safe_raster_source(path_or_url: str, http_options: dict[str, str] | None = None) -> Iterator[str]:
    """Yield a source path/URL while routing remote COG ranges through pinned HTTPS.

    GDAL sees only a tokenized loopback HTTP endpoint. Each byte range is fetched
    through ``public_http`` with DNS pinning, redirects disabled, bounded bytes,
    timeout and retry policy. Large COGs are read by range without downloading
    the complete object. The loopback server is private to this context and is
    stopped as soon as the caller closes the raster.
    """
    source = str(path_or_url)
    hostname = validate_remote_raster_url(source, http_options)
    if hostname is None:
        yield source
        return

    headers = _raster_http_headers(http_options)
    probe = public_http.get(
        source,
        headers={**headers, "Range": "bytes=0-0"},
        timeout=(3, 8),
        max_bytes=1,
    )
    content_range = str(probe.headers.get("Content-Range", ""))
    match = re.fullmatch(r"bytes 0-0/(\d+)", content_range.strip(), re.IGNORECASE)
    if probe.status_code != 206 or not match or len(probe.content) != 1:
        raise ValueError("Remote raster must support bounded HTTPS byte-range reads.")
    object_size = int(match.group(1))
    if object_size <= 0 or object_size > MAX_REMOTE_RASTER_OBJECT_BYTES:
        raise ValueError("Remote raster object exceeds the configured size limit.")
    etag = str(probe.headers.get("ETag") or "").strip()
    last_modified = str(probe.headers.get("Last-Modified") or "").strip()
    token = secrets.token_urlsafe(32)
    state = {"bytes_served": 0, "bytes_reserved": 0}
    state_lock = threading.Lock()

    class RangeProxyHandler(BaseHTTPRequestHandler):
        # Each bounded range is served on a short-lived connection. HTTP/1.0
        # makes the close behavior explicit for GDAL's VSI curl client.
        protocol_version = "HTTP/1.0"
        server_version = ""
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            # The request path contains a bearer-like token; never log it.
            return

        def _authorized(self) -> bool:
            return self.path == f"/{token}.tif"

        def _reply(self, status: int, body: bytes = b"", extra_headers: dict[str, str] | None = None) -> None:
            outgoing_headers = extra_headers or {}
            self.send_response(status)
            self.send_header("Content-Length", outgoing_headers.get("Content-Length", str(len(body))))
            self.send_header("Content-Type", "image/tiff")
            for name, value in outgoing_headers.items():
                if name.lower() != "content-length":
                    self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            if body and self.command != "HEAD":
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_HEAD(self) -> None:
            if not self._authorized():
                self._reply(404)
                return
            metadata = {"Accept-Ranges": "bytes"}
            if etag:
                metadata["ETag"] = etag
            if last_modified:
                metadata["Last-Modified"] = last_modified
            self._reply(200, extra_headers={**metadata, "Content-Length": str(object_size)})

        def do_GET(self) -> None:
            if not self._authorized():
                self._reply(404)
                return

            range_value = str(self.headers.get("Range") or "").strip()
            whole_object = not range_value
            if whole_object:
                if object_size > MAX_REMOTE_RASTER_RANGE_BYTES:
                    self._reply(416, extra_headers={"Content-Range": f"bytes */{object_size}"})
                    return
                start, end = 0, object_size - 1
            else:
                range_match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_value, re.IGNORECASE)
                if not range_match:
                    self._reply(416, extra_headers={"Content-Range": f"bytes */{object_size}"})
                    return
                start, requested_end = (int(value) for value in range_match.groups())
                if start >= object_size or requested_end < start:
                    self._reply(416, extra_headers={"Content-Range": f"bytes */{object_size}"})
                    return
                end = min(requested_end, object_size - 1)

            byte_count = end - start + 1
            if byte_count <= 0 or byte_count > MAX_REMOTE_RASTER_RANGE_BYTES:
                self._reply(416, extra_headers={"Content-Range": f"bytes */{object_size}"})
                return

            if_range = str(self.headers.get("If-Range") or "").strip()
            # If-Range requires a strong entity tag. Weak ETags are unsuitable;
            # use Last-Modified in that case when the provider supplies it.
            validator = (etag if etag and not etag.lstrip().startswith("W/") else "") or last_modified
            if if_range and validator and if_range != validator:
                self._reply(412)
                return

            request_headers = {**headers, "Range": f"bytes={start}-{end}"}
            if validator:
                request_headers["If-Range"] = validator
            with state_lock:
                over_session_limit = (
                    state["bytes_served"] + state["bytes_reserved"] + byte_count
                    > MAX_REMOTE_RASTER_SESSION_BYTES
                )
                if not over_session_limit:
                    state["bytes_reserved"] += byte_count
            if over_session_limit:
                self._reply(413)
                return
            reservation_active = True
            try:
                upstream = public_http.get(
                    source,
                    headers=request_headers,
                    timeout=(3, 20),
                    max_bytes=byte_count,
                )
                upstream_range = str(upstream.headers.get("Content-Range", ""))
                expected_range = f"bytes {start}-{end}/{object_size}"
                if (upstream.status_code != 206 or upstream_range.strip().lower() != expected_range.lower()
                        or len(upstream.content) != byte_count):
                    raise ValueError("Remote raster did not return the exact requested byte range")
                if etag and upstream.headers.get("ETag") and str(upstream.headers["ETag"]).strip() != etag:
                    raise ValueError("Remote raster entity changed during range reads")
                with state_lock:
                    state["bytes_reserved"] -= byte_count
                    state["bytes_served"] += byte_count
                reservation_active = False
                metadata = {"Accept-Ranges": "bytes"}
                if not whole_object:
                    metadata["Content-Range"] = expected_range
                if etag:
                    metadata["ETag"] = etag
                if last_modified:
                    metadata["Last-Modified"] = last_modified
                self._reply(200 if whole_object else 206, upstream.content, metadata)
            except Exception as exc:  # noqa: BLE001 - keep remote errors and signed URLs private
                if reservation_active:
                    with state_lock:
                        state["bytes_reserved"] -= byte_count
                logger.warning("Bounded raster range failed host=%s error_type=%s", hostname, type(exc).__name__)
                self._reply(502)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RangeProxyHandler)
    server.daemon_threads = True
    server.block_on_close = False
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    local_url = f"http://127.0.0.1:{server.server_address[1]}/{token}.tif"
    try:
        import rasterio

        with rasterio.Env(
            GDAL_HTTP_PROXY="",
            GDAL_HTTPS_PROXY="",
            GDAL_HTTP_TIMEOUT=25,
            GDAL_HTTP_CONNECTTIMEOUT=5,
            GDAL_HTTP_MAX_RETRY=0,
            GDAL_HTTP_NETRC="NO",
            # Avoid GDAL directory listings and sidecar probes against this
            # single-object endpoint; only the authorized TIFF is exposed.
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        ):
            yield local_url
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class GridSpec:
    """Common target grid shared by every provider so S2/DEM/WorldCover/label
    rasters all land on identical pixel grids without separate resampling
    passes downstream.
    """

    def __init__(self, bbox_lonlat: BBox, resolution_m: float, dst_crs: str | None = None):
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
        if self.width <= 0 or self.height <= 0 or self.width * self.height > MAX_GRID_PIXELS:
            raise ValueError(f"Raster grid exceeds the {MAX_GRID_PIXELS:,}-pixel workload limit.")
        self.shape = (self.height, self.width)

    def to_affine_and_shape(self):
        return self.transform, self.width, self.height, self.crs


def load_raster_to_grid(
    path_or_url: str,
    grid: GridSpec,
    band_index: int = 1,
    resampling: str = "bilinear",
    src_nodata: float | None = None,
    http_options: dict[str, str] | None = None,
) -> np.ndarray | None:
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
    safe_host = None
    try:
        safe_host = validate_remote_raster_url(path_or_url, http_options)
        with safe_raster_source(path_or_url, http_options) as local_path, rasterio.open(local_path) as src:
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
    except Exception as exc:  # noqa: BLE001 - raster read can fail many ways; caller treats None as "unavailable"
        if http_options is not None:
            logger.warning("Configured carbon raster could not be read (%s)", type(exc).__name__)
        else:
            logger.warning("load_raster_to_grid: remote/local raster unavailable host=%s error_type=%s",
                           safe_host or "local", type(exc).__name__)
        return None


def build_stack_from_local_files(file_map: dict[str, str], grid: GridSpec) -> NonGeeStack:
    """file_map e.g. {'B2': 'path/b2.tif', ..., 'elevation': 'dem.tif'}.

    Used for (a) local test fixtures when validating grid-alignment logic
    without hitting Planetary Computer, and (b) any locally-cached composite
    reused across repeated experiments.
    """
    bands: dict[str, np.ndarray] = {}
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

def _enumerate_tile_origins(bbox: BBox, tile_size_deg: int = 10) -> list[tuple[int, int]]:
    """South-west-corner tile origins (floor convention) intersecting bbox."""
    west, south, east, north = bbox
    lat_start = math.floor(south / tile_size_deg) * tile_size_deg
    lon_start = math.floor(west / tile_size_deg) * tile_size_deg
    lats = range(lat_start, math.ceil(north / tile_size_deg) * tile_size_deg, tile_size_deg)
    lons = range(lon_start, math.ceil(east / tile_size_deg) * tile_size_deg, tile_size_deg)
    return [(la, lo) for la in lats for lo in lons]


def _hansen_tile_url(lat_origin: int, lon_origin: int, template: str, tile_size_deg: int = 10) -> str:
    from app.providers.external_carbon_provider import _tile_key
    lat_str, lon_str = _tile_key(lat_origin + tile_size_deg / 2, lon_origin + tile_size_deg / 2, tile_size_deg)
    return template.format(lat_tile=lat_str, lon_tile=lon_str)


def _ceda_tile_url(lat_origin: int, lon_origin: int, template: str, year: int, northwest: bool = False) -> str:
    # v7 filenames use the north-west corner; enumeration uses south-west.
    if northwest:
        lat_origin += 10
    ns = "N" if lat_origin >= 0 else "S"
    ew = "E" if lon_origin >= 0 else "W"
    tile_id = f"{ns}{abs(lat_origin):02d}{ew}{abs(lon_origin):03d}"
    return template.format(tile_id=tile_id, year=year)


def _apply_transform_array(arr: np.ndarray, transform: str) -> np.ndarray:
    """Array-safe version of external_carbon_provider._apply_transform
    (that one is scalar-only: `if value is None` breaks on ndarrays)."""
    if transform == "divide_10":
        return arr / 10.0
    if transform == "divide_10_multiply_0.47":
        return (arr / 10.0) * 0.47
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
    nodata: float | None = None,
    resampling: str = "bilinear",
    lossyear_url_builder: Callable[[int, int], str] | None = None,
) -> tuple[np.ndarray, int]:
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
    dataset_key: str, grid: GridSpec, year: int | None = None,
) -> tuple[np.ndarray, dict]:
    """Load a non-GEE-sampleable carbon reference dataset from
    CARBON_EXTERNAL_REGISTRY onto `grid`, applying the registry's unit
    transform. Returns (label_array[H,W] in Mg C/ha with NaN outside
    coverage, info dict with n_tiles_used/transform/unit for provenance).

    Supported today: HANSEN_TREECOVER_AGB_PROXY, ESA_CCI_BIOMASS_COG,
    ESA_CCI_BIOMASS_V7_COG, and CTREES_AGB_100M.
    """
    from app.registries.carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY

    meta = CARBON_EXTERNAL_REGISTRY.get(dataset_key)
    if meta is None:
        raise ValueError(f"'{dataset_key}' not found in CARBON_EXTERNAL_REGISTRY")
    if dataset_key == "CHLORIS_AGB_STOCK":
        from app.services.chloris_service import (
            ChlorisUnavailableError, chloris_raster_http_options, resolve_chloris_download,
        )

        download = resolve_chloris_download(year=year)
        if download.metadata.get("source") == "planetary_computer_stac":
            raise ChlorisUnavailableError("Raster lokal Chloris memerlukan sumber berlisensi dengan unit t/ha.")
        mosaic = load_raster_to_grid(
            download.url, grid, resampling="bilinear",
            http_options=chloris_raster_http_options(download.url),
        )
        if mosaic is None:
            raise ChlorisUnavailableError("Raster Chloris berlisensi tidak dapat dibaca; periksa URL dan hak akses.")
        return _apply_transform_array(mosaic, meta.get("transform", "multiply_0.47")), {
            "dataset_key": dataset_key, "n_tiles_used": 1,
            "transform": meta.get("transform"), "unit": meta.get("unit"),
            "target_pool": meta.get("target_pool"),
            "label_year": download.metadata.get("year"),
            "source": download.metadata.get("source"),
        }
    if not meta.get("is_tiled") and not meta.get("global_url_template"):
        raise NotImplementedError(
            f"load_carbon_reference_to_grid only supports tiled/global COG datasets currently; "
            f"'{dataset_key}' has no tile_url_template or global_url_template"
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
    elif dataset_key in {"ESA_CCI_BIOMASS_COG", "ESA_CCI_BIOMASS_V7_COG"}:
        target_year = year or meta.get("year", 2020)
        available_years = meta.get("available_years") or _CEDA_AVAILABLE_YEARS
        nearest_year = min(available_years, key=lambda y: abs(y - target_year))
        if nearest_year != target_year:
            logger.info(f"{dataset_key}: year {target_year} unavailable, using nearest available {nearest_year}")
        url_builder = lambda la, lo: _ceda_tile_url(la, lo, meta["tile_url_template"], nearest_year, meta.get("tile_origin") == "northwest")
        mosaic, n_tiles = _mosaic_tiled_cog(url_builder, grid, nodata=meta.get("nodata"), resampling="bilinear")
    elif dataset_key == "CTREES_AGB_100M":
        target_year = year or meta.get("year", 2025)
        available_years = meta.get("available_years") or []
        nearest_year = min(available_years, key=lambda y: abs(y - target_year)) if available_years else target_year
        if nearest_year != target_year:
            logger.info(f"CTREES_AGB_100M: year {target_year} unavailable, using nearest available {nearest_year}")
        url = meta["global_url_template"].format(year=nearest_year)
        mosaic = load_raster_to_grid(url, grid, resampling="bilinear", src_nodata=meta.get("nodata"))
        n_tiles = 1 if mosaic is not None else 0
        if mosaic is None:
            mosaic = np.full(grid.shape, np.nan, dtype=np.float32)
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
        "label_year": (
            nearest_year
            if dataset_key in {"ESA_CCI_BIOMASS_COG", "ESA_CCI_BIOMASS_V7_COG", "CTREES_AGB_100M"}
            else meta.get("year")
        ),
    }


def _xyz_bounds_wgs84(z: int, x: int, y: int) -> BBox:
    """Return the geographic bounds of an XYZ/Web-Mercator tile."""
    n = 2 ** z
    x = x % n
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _carbon_tile_urls(dataset_key: str, bbox: BBox, year: int | None) -> list[str]:
    """Build only the public COG URLs intersecting one map tile."""
    from app.registries.carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY

    meta = CARBON_EXTERNAL_REGISTRY[dataset_key]
    urls: list[str] = []
    if dataset_key == "HANSEN_TREECOVER_AGB_PROXY":
        for lat_o, lon_o in _enumerate_tile_origins(bbox, int(meta.get("tile_size_deg", 10))):
            urls.append(_hansen_tile_url(lat_o, lon_o, meta["tile_url_template"], int(meta.get("tile_size_deg", 10))))
    elif dataset_key in {"ESA_CCI_BIOMASS_COG", "ESA_CCI_BIOMASS_V7_COG"}:
        available = meta.get("available_years") or [meta.get("year", 2020)]
        target = year or meta.get("year", 2020)
        selected = min(available, key=lambda item: abs(item - target))
        for lat_o, lon_o in _enumerate_tile_origins(bbox, int(meta.get("tile_size_deg", 10))):
            urls.append(_ceda_tile_url(lat_o, lon_o, meta["tile_url_template"], selected, meta.get("tile_origin") == "northwest"))
    elif dataset_key == "CTREES_AGB_100M":
        available = meta.get("available_years") or [meta.get("year", 2025)]
        target = year or meta.get("year", 2025)
        selected = min(available, key=lambda item: abs(item - target))
        urls.append(meta["global_url_template"].format(year=selected))
    return urls


def render_carbon_reference_tile(
    dataset_key: str,
    z: int,
    x: int,
    y: int,
    year: int | None = None,
    vis_min: float | None = None,
    vis_max: float | None = None,
) -> bytes | None:
    """Render a 256px transparent PNG from a public COG reference.

    Reads only the requested Web-Mercator window through WarpedVRT, so global
    CTrees files remain practical and no full raster is downloaded.
    """
    started = time.perf_counter()
    _tile_stat("requests")
    from io import BytesIO

    from PIL import Image
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.vrt import WarpedVRT

    from app.registries.carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY

    if dataset_key not in CARBON_EXTERNAL_REGISTRY or z < 0 or z > 22 or y < 0 or y >= 2 ** z:
        return None
    chloris_download = None
    http_options: dict[str, str] = {}
    source_identity = ""
    if dataset_key == "CHLORIS_AGB_STOCK":
        from app.services.chloris_service import (
            ChlorisUnavailableError, chloris_raster_http_options, resolve_chloris_download,
        )

        chloris_download = resolve_chloris_download(year=year)
        if chloris_download.metadata.get("source") == "planetary_computer_stac":
            raise ChlorisUnavailableError("Gunakan tile Planetary Computer untuk Chloris publik 2003-2019.")
        http_options = chloris_raster_http_options(chloris_download.url)
        source_identity = hashlib.sha256(chloris_download.url.encode()).hexdigest()
    cache_key = f"{dataset_key}:{z}:{x % (2 ** z)}:{y}:{year}:{vis_min}:{vis_max}"
    if source_identity:
        cache_key += f":{source_identity}"
    with _CARBON_TILE_CACHE_LOCK:
        cached = _read_cached_carbon_tile(cache_key)
    if cached is not None:
        _tile_stat("cache_hits")
        logger.info("carbon COG tile cache hit dataset=%s z=%s x=%s y=%s", dataset_key, z, x, y)
        return cached
    _tile_stat("cache_misses")
    meta = CARBON_EXTERNAL_REGISTRY[dataset_key]
    bbox = _xyz_bounds_wgs84(z, x, y)
    n = 2 ** z
    x = x % n
    r = 6378137.0
    west, south, east, north = bbox
    mercator_bounds = (
        math.radians(west) * r,
        math.log(math.tan(math.pi / 4 + math.radians(south) / 2)) * r,
        math.radians(east) * r,
        math.log(math.tan(math.pi / 4 + math.radians(north) / 2)) * r,
    )
    dst_transform = from_bounds(*mercator_bounds, 256, 256)
    canvas = np.full((256, 256), np.nan, dtype=np.float32)
    urls = [chloris_download.url] if chloris_download else _carbon_tile_urls(dataset_key, bbox, year)
    if not urls:
        return None
    from app.core.config import get_settings
    max_sources = max(1, int(getattr(get_settings(), "carbon_tile_max_source_tiles", 32)))
    if len(urls) > max_sources:
        _tile_stat("source_limit_skips")
        logger.warning(
            "carbon COG tile skipped dataset=%s z=%s x=%s y=%s sources=%s limit=%s; zoom in for detail",
            dataset_key, z, x, y, len(urls), max_sources,
        )
        return None
    logger.info("carbon COG tile render start dataset=%s z=%s x=%s y=%s sources=%s", dataset_key, z, x, y, len(urls))

    for url in urls:
        _tile_stat("source_reads")
        raster_host = None
        try:
            raster_host = validate_remote_raster_url(url)
            with safe_raster_source(url, http_options) as local_path, rasterio.open(local_path) as src:
                src_nodata = meta.get("nodata") if meta.get("nodata") is not None else src.nodata
                with WarpedVRT(
                    src,
                    crs="EPSG:3857",
                    transform=dst_transform,
                    width=256,
                    height=256,
                    resampling=Resampling.bilinear,
                    src_nodata=src_nodata,
                    nodata=np.nan,
                ) as vrt:
                    raw = vrt.read(1).astype(np.float32)
                raw = _apply_transform_array(raw, meta.get("transform", "none"))
                valid = np.isfinite(raw)
                canvas[np.isnan(canvas) & valid] = raw[np.isnan(canvas) & valid]
                if np.isfinite(canvas).all():
                    break
        except Exception as exc:  # noqa: BLE001 - missing ocean tiles are expected
            if chloris_download:
                raise ValueError("Raster Chloris berlisensi tidak dapat dibaca.") from exc
            logger.debug(
                "carbon tile source unavailable dataset=%s host=%s error_type=%s",
                dataset_key,
                raster_host or "unknown",
                type(exc).__name__,
            )

    if not np.isfinite(canvas).any():
        _tile_stat("empty")
        logger.info("carbon COG tile empty dataset=%s z=%s x=%s y=%s elapsed_ms=%.1f", dataset_key, z, x, y, (time.perf_counter() - started) * 1000)
        return None
    lo = float(vis_min if vis_min is not None else meta.get("vis_min", 0))
    hi = float(vis_max if vis_max is not None else meta.get("vis_max", 300))
    if hi <= lo:
        hi = lo + 1
    normalized = np.clip((canvas - lo) / (hi - lo), 0, 1)
    palette = meta.get("vis_palette") or ["ffffff", "006d2c"]
    rgb = np.zeros((256, 256, 3), dtype=np.uint8)
    for channel in range(3):
        stops = np.array([int(color.lstrip("#")[channel * 2:channel * 2 + 2], 16) for color in palette], dtype=float)
        positions = normalized * (len(stops) - 1)
        low = np.floor(positions).astype(int).clip(0, len(stops) - 1)
        high = np.ceil(positions).astype(int).clip(0, len(stops) - 1)
        fraction = positions - low
        rgb[:, :, channel] = np.where(np.isfinite(canvas), stops[low] * (1 - fraction) + stops[high] * fraction, 0).astype(np.uint8)
    alpha = np.where(np.isfinite(canvas), 220, 0).astype(np.uint8)
    image = Image.fromarray(np.dstack([rgb, alpha]), mode="RGBA")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    content = output.getvalue()
    with _CARBON_TILE_CACHE_LOCK:
        _write_cached_carbon_tile(cache_key, content)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _tile_stat("rendered")
    _tile_stat("total_render_ms", elapsed_ms)
    logger.info("carbon COG tile rendered dataset=%s z=%s x=%s y=%s sources=%s elapsed_ms=%.1f", dataset_key, z, x, y, len(urls), elapsed_ms)
    return content


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
    import os
    import tempfile

    import rasterio
    from rasterio.transform import from_bounds

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
