"""
external_carbon_provider.py — Abstraction layer for external raster carbon providers.

Handles provider_type = "external_raster" datasets from CARBON_EXTERNAL_REGISTRY.

Currently implemented ingestion methods:
  soilgrids_rest  — ISRIC SoilGrids v2.0 REST API, point sampling
  cog_rasterio    — Cloud-Optimized GeoTIFF via rasterio (optional dep)

Add new methods by:
  1. Adding an entry to CARBON_EXTERNAL_REGISTRY with a new ingestion_method value.
  2. Implementing _sample_<ingestion_method>() and _stats_<ingestion_method>() here.
"""
from __future__ import annotations

import logging
import math
import random
import time

import requests

logger = logging.getLogger(__name__)

# Max AOI sample points used for reference statistics (rate-limit safe)
_AOI_STATS_N_SAMPLES = 80
# Max retry attempts per point on transient HTTP errors
_HTTP_RETRIES = 3
_SOILGRIDS_CACHE_TTL_SECONDS = 3600
_SOILGRIDS_CACHE_MAX_ITEMS = 1024
_SOILGRIDS_CACHE: dict[tuple[float, float], tuple[float, float | None]] = {}
_SOILGRIDS_COMPONENT_CACHE: dict[tuple[float, float], tuple[float, tuple[float | None, float | None]]] = {}


# ─────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────

class ExternalRasterProvider:
    """
    Dispatch class for external raster carbon dataset operations.

    All methods accept a metadata dict from CARBON_EXTERNAL_REGISTRY and
    route to the appropriate implementation based on ingestion_method.
    """

    def sample_carbon_labels(
        self,
        meta: dict,
        points_lonlat: list[tuple[float, float]],
    ) -> list[float | None]:
        """
        Fetch carbon/SOC values at given (lon, lat) points from the provider.

        Returns:
            List of float values (native units, before transform) or None per point.

        Raises:
            NotImplementedError: if ingestion_method is not yet implemented.
            RuntimeError: on unrecoverable API errors.
        """
        method = meta.get("ingestion_method", "")
        if method == "soilgrids_rest":
            return _sample_soilgrids(meta, points_lonlat)
        if method == "cog_rasterio":
            return _sample_cog_rasterio(meta, points_lonlat)
        raise NotImplementedError(
            f"No label sampler for ingestion_method='{method}'. "
            "Implement _sample_{method}() in external_carbon_provider.py."
        )

    def get_stats_for_aoi(
        self,
        meta: dict,
        aoi_geojson: dict,
        n_samples: int = _AOI_STATS_N_SAMPLES,
        seed: int = 42,
    ) -> dict:
        """
        Compute carbon statistics for the given AOI by sampling N random points.

        Returns dict with: mean, std_dev, min, max, n_samples, unit, note.

        Raises:
            NotImplementedError: if provider does not support point sampling.
            RuntimeError: if too few valid samples returned.
        """
        bbox = _geojson_to_bbox(aoi_geojson)
        west, south, east, north = bbox

        points = _sample_aoi_points(aoi_geojson, bbox, n_samples, seed)

        if meta.get("ingestion_method") == "soilgrids_rest":
            components = _sample_soilgrids_components(meta, points)
            values = [value[0] for value in components if value[0] is not None]
            stock_values = [value[1] for value in components if value[1] is not None]
            if len(values) < 5:
                raise RuntimeError(f"External provider '{meta.get('key')}' returned only {len(values)} valid values out of {n_samples} sample points in AOI.")
            result = _summarize_values(values, meta)
            if stock_values:
                result.update(_summarize_values(stock_values, {**meta, "unit": "Mg C/ha"}, prefix="stock_"))
            return result

        raw_values = self.sample_carbon_labels(meta, points)

        transform = meta.get("transform", "none")
        values = [_apply_transform(v, transform) for v in raw_values if v is not None]

        if len(values) < 5:
            raise RuntimeError(
                f"External provider '{meta.get('key')}' returned only {len(values)} valid "
                f"values out of {n_samples} sample points in AOI. "
                "Check API availability or enlarge AOI."
            )

        mean_v = sum(values) / len(values)
        variance = sum((v - mean_v) ** 2 for v in values) / len(values)
        std_v = math.sqrt(variance)

        return {
            "mean":      round(mean_v, 3),
            "std_dev":   round(std_v, 3),
            "min":       round(min(values), 3),
            "max":       round(max(values), 3),
            "n_samples": len(values),
            "unit":      meta.get("unit", ""),
            "provider":  meta.get("provider_type"),
        }

    def get_tile_url(self, meta: dict, dataset_key: str) -> str | None:
        """External raster providers expose no direct tile URL here."""
        logger.info(
            "Tile rendering not available for external provider '%s' (ingestion_method='%s').",
            dataset_key,
            meta.get("ingestion_method"),
        )
        return None


def _sample_aoi_points(aoi_geojson: dict, bbox: tuple[float, float, float, float], n_samples: int, seed: int) -> list[tuple[float, float]]:
    west, south, east, north = bbox
    rng = random.Random(seed)
    try:
        from shapely.geometry import Point, shape
        geometry_payload = aoi_geojson.get("geometry") if aoi_geojson.get("type") == "Feature" else aoi_geojson
        geometry = shape(geometry_payload)
    except Exception:  # noqa: BLE001
        geometry = None
    points: list[tuple[float, float]] = []
    attempts = 0
    while len(points) < n_samples and attempts < max(n_samples * 20, 100):
        attempts += 1
        candidate = (rng.uniform(west, east), rng.uniform(south, north))
        if geometry is None or geometry.contains(Point(*candidate)) or geometry.touches(Point(*candidate)):
            points.append(candidate)
    return points if len(points) >= 5 else [(rng.uniform(west, east), rng.uniform(south, north)) for _ in range(n_samples)]


def _summarize_values(values: list[float], meta: dict, prefix: str = "") -> dict:
    mean_v = sum(values) / len(values)
    return {
        f"{prefix}mean": round(mean_v, 3),
        f"{prefix}std_dev": round(math.sqrt(sum((v - mean_v) ** 2 for v in values) / len(values)), 3),
        f"{prefix}min": round(min(values), 3),
        f"{prefix}max": round(max(values), 3),
        f"{prefix}n_samples": len(values),
        f"{prefix}unit": meta.get("unit", ""),
        "provider": meta.get("provider_type"),
    }

# ─────────────────────────────────────────────
# SoilGrids REST ingestion
# ─────────────────────────────────────────────

def _sample_soilgrids(
    meta: dict,
    points: list[tuple[float, float]],
) -> list[float | None]:
    """
    Query ISRIC SoilGrids v2.0 REST API for SOC at each (lon, lat) point.

    Returns depth-weighted mean SOC in dg/kg (native units).
    Provider transform "divide_10" converts to g/kg downstream.

    Depths used: 0-5cm (weight 5), 5-15cm (weight 10), 15-30cm (weight 15)
    Total depth: 30 cm.
    """
    service_url = meta.get("service_url", "https://rest.isric.org/soilgrids/v2.0/properties/query")
    depth_weights = [("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15)]
    total_depth = sum(w for _, w in depth_weights)

    values: list[float | None] = []
    for lon, lat in points:
        cache_key = (round(lon, 5), round(lat, 5))
        cached = _SOILGRIDS_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < _SOILGRIDS_CACHE_TTL_SECONDS:
            values.append(cached[1])
            continue
        val = _soilgrids_point(service_url, lon, lat, depth_weights, total_depth)
        if len(_SOILGRIDS_CACHE) >= _SOILGRIDS_CACHE_MAX_ITEMS:
            # Drop the oldest entry without introducing a dependency on a
            # cache service; this endpoint is also used in local deployments.
            oldest = min(_SOILGRIDS_CACHE, key=lambda key: _SOILGRIDS_CACHE[key][0])
            _SOILGRIDS_CACHE.pop(oldest, None)
        _SOILGRIDS_CACHE[cache_key] = (time.monotonic(), val)
        values.append(val)
        # Polite rate limiting: 10 req/s max recommended by ISRIC
        time.sleep(0.12)

    valid = sum(1 for v in values if v is not None)
    logger.info(f"SoilGrids sampled {len(points)} points → {valid} valid")
    return values


def _sample_soilgrids_components(meta: dict, points: list[tuple[float, float]]) -> list[tuple[float | None, float | None]]:
    """Return (SOC g/kg, 0-30cm stock Mg C/ha) for each point."""
    service_url = meta.get("service_url", "https://rest.isric.org/soilgrids/v2.0/properties/query")
    depths = [("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15)]
    values: list[tuple[float | None, float | None]] = []
    for lon, lat in points:
        key = (round(lon, 5), round(lat, 5))
        cached = _SOILGRIDS_COMPONENT_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _SOILGRIDS_CACHE_TTL_SECONDS:
            values.append(cached[1])
            continue
        result = _soilgrids_components_point(service_url, lon, lat, depths)
        if len(_SOILGRIDS_COMPONENT_CACHE) >= _SOILGRIDS_CACHE_MAX_ITEMS:
            oldest = min(_SOILGRIDS_COMPONENT_CACHE, key=lambda item: _SOILGRIDS_COMPONENT_CACHE[item][0])
            _SOILGRIDS_COMPONENT_CACHE.pop(oldest, None)
        _SOILGRIDS_COMPONENT_CACHE[key] = (time.monotonic(), result)
        values.append(result)
        time.sleep(0.12)
    return values


def _soilgrids_components_point(
    service_url: str,
    lon: float,
    lat: float,
    depths: list[tuple[str, int]],
) -> tuple[float | None, float | None]:
    params = {
        "lon": lon,
        "lat": lat,
        "property": ["soc", "bdod"],
        "depth": [label for label, _ in depths],
        "value": "mean",
    }
    for attempt in range(_HTTP_RETRIES):
        try:
            response = requests.get(service_url, params=params, timeout=30)
            if response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            layers = {layer.get("name"): layer for layer in response.json().get("properties", {}).get("layers", [])}
            soc_map = _soilgrids_depth_map(layers.get("soc"))
            bd_map = _soilgrids_depth_map(layers.get("bdod"))
            soc_values = [soc_map.get(label) for label, _ in depths]
            valid_soc = [(raw, thickness) for raw, (_, thickness) in zip(soc_values, depths) if raw is not None]
            soc_gkg = (sum(raw * thickness for raw, thickness in valid_soc) / sum(thickness for _, thickness in valid_soc) / 10.0) if valid_soc else None
            stock = None
            if all(soc_map.get(label) is not None and bd_map.get(label) is not None for label, _ in depths):
                # SOC is dg/kg (÷10 → g/kg); bdod is cg/cm³ (÷100 → g/cm³).
                stock = sum((soc_map[label] / 10.0) * (bd_map[label] / 100.0) * thickness / 10.0 for label, thickness in depths)
            return soc_gkg, stock
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            if attempt < _HTTP_RETRIES - 1:
                time.sleep(1.0)
            else:
                logger.debug("SoilGrids components (%s,%s) failed: %s", lon, lat, exc)
    return None, None


def _soilgrids_depth_map(layer: dict | None) -> dict[str, float | None]:
    if not layer:
        return {}
    output: dict[str, float | None] = {}
    for depth in layer.get("depths", []):
        raw = depth.get("values", {}).get("mean")
        output[depth.get("label", "")] = float(raw) if raw is not None else None
    return output


def _soilgrids_point(
    service_url: str,
    lon: float,
    lat: float,
    depth_weights: list[tuple[str, int]],
    total_depth: int,
) -> float | None:
    """Query one point from SoilGrids REST, return weighted-mean SOC (dg/kg)."""
    params = {
        "lon":      lon,
        "lat":      lat,
        "property": "soc",
        "depth":    [d for d, _ in depth_weights],
        "value":    "mean",
    }
    for attempt in range(_HTTP_RETRIES):
        try:
            resp = requests.get(service_url, params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            return _parse_soilgrids_response(data, depth_weights, total_depth)
        except (requests.RequestException, ValueError, KeyError) as exc:
            if attempt < _HTTP_RETRIES - 1:
                time.sleep(1.0)
            else:
                logger.debug(f"SoilGrids point ({lon:.4f},{lat:.4f}) failed after {_HTTP_RETRIES} tries: {exc}")
    return None


def _parse_soilgrids_response(
    data: dict,
    depth_weights: list[tuple[str, int]],
    total_depth: int,
) -> float | None:
    """Extract depth-weighted mean SOC (dg/kg) from SoilGrids API response."""
    try:
        layers = data["properties"]["layers"]
        soc_layer = next(
            (l for l in layers if l.get("name") == "soc"), None
        )
        if soc_layer is None:
            return None

        depth_map: dict[str, float | None] = {}
        for depth_obj in soc_layer.get("depths", []):
            label = depth_obj.get("label", "")
            raw = depth_obj.get("values", {}).get("mean")
            depth_map[label] = float(raw) if raw is not None else None

        weighted = 0.0
        valid_weight = 0
        for depth_label, weight in depth_weights:
            v = depth_map.get(depth_label)
            if v is not None:
                weighted += v * weight
                valid_weight += weight

        if valid_weight == 0:
            return None
        return weighted / valid_weight  # dg/kg

    except (KeyError, TypeError, StopIteration):
        return None


# ─────────────────────────────────────────────
# COG/rasterio ingestion (optional dep)
# ─────────────────────────────────────────────

def _sample_cog_rasterio(
    meta: dict,
    points: list[tuple[float, float]],
) -> list[float | None]:
    """
    Sample a Cloud-Optimized GeoTIFF via rasterio.

    Handles both single-file COGs (service_url) and tiled COGs (is_tiled=True +
    tile_url_template), e.g. Hansen GFC 10°×10° tiles.

    Requires: rasterio, pyproj (optional dependencies).
    Raises NotImplementedError if rasterio is not installed.
    """
    try:
        import rasterio
    except ImportError:
        raise NotImplementedError(
            "COG/rasterio ingestion requires the 'rasterio' package. "
            "Install it with: pip install rasterio. "
            f"Dataset '{meta.get('key')}' uses ingestion_method='cog_rasterio'."
        )

    if meta.get("is_tiled"):
        return _sample_cog_tiled(meta, points)

    cog_url = meta.get("service_url")
    if not cog_url:
        raise NotImplementedError(
            f"Dataset '{meta.get('key')}' has no service_url configured for COG access. "
            "Set service_url in CARBON_EXTERNAL_REGISTRY to a publicly accessible COG URL."
        )

    import rasterio
    from rasterio.crs import CRS

    nodata_cfg = meta.get("nodata")
    values: list[float | None] = []
    with rasterio.open(cog_url) as src:
        nodata = src.nodata if nodata_cfg is None else nodata_cfg
        crs = src.crs
        wgs84 = CRS.from_epsg(4326)

        try:
            from pyproj import Transformer
            to_native = Transformer.from_crs(wgs84, crs, always_xy=True)
        except ImportError:
            to_native = None

        for lon, lat in points:
            try:
                if to_native and crs != wgs84:
                    x, y = to_native.transform(lon, lat)
                else:
                    x, y = lon, lat
                row, col = src.index(x, y)
                if not (0 <= row < src.height and 0 <= col < src.width):
                    values.append(None)
                    continue
                raw = float(src.read(1, window=((row, row + 1), (col, col + 1)))[0, 0])
                values.append(None if (nodata is not None and raw == nodata) else raw)
            except Exception:  # noqa: BLE001 - one bad point shouldn't fail the whole batch; sampled as missing
                values.append(None)

    valid = sum(1 for v in values if v is not None)
    logger.info(f"COG rasterio sampled {len(points)} points → {valid} valid")
    return values


def _sample_cog_tiled(
    meta: dict,
    points: list[tuple[float, float]],
) -> list[float | None]:
    """
    Sample a tiled COG where the URL is built per tile from coordinates.

    Designed for Hansen GFC (10°×10° tiles, HTTPS public GCS).
    Registry fields used: tile_url_template, tile_size_deg, nodata.
    """
    import rasterio
    from rasterio.crs import CRS

    tile_url_template = meta.get("tile_url_template", "")
    tile_size_deg = int(meta.get("tile_size_deg", 10))
    nodata_cfg = meta.get("nodata")

    # Group point indices by tile key
    tile_groups: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
    for i, (lon, lat) in enumerate(points):
        key = _tile_key(lat, lon, tile_size_deg)
        tile_groups.setdefault(key, []).append((i, lon, lat))

    values: list[float | None] = [None] * len(points)

    lossyear_template = meta.get("lossyear_url_template")
    mask_lossyear = meta.get("mask_lossyear", False) and bool(lossyear_template)

    for (lat_str, lon_str), tile_pts in tile_groups.items():
        url = tile_url_template.format(lat_tile=lat_str, lon_tile=lon_str)
        lossyear_url = (
            lossyear_template.format(lat_tile=lat_str, lon_tile=lon_str)
            if mask_lossyear else None
        )
        try:
            with rasterio.open(url) as src:
                tnd = src.nodata if nodata_cfg is None else nodata_cfg
                wgs84 = CRS.from_epsg(4326)
                try:
                    from pyproj import Transformer
                    to_native = (
                        Transformer.from_crs(wgs84, src.crs, always_xy=True)
                        if src.crs != wgs84 else None
                    )
                except ImportError:
                    to_native = None

                # Optional: open lossyear tile to mask temporally inconsistent pixels
                loss_src = None
                if lossyear_url:
                    try:
                        loss_src = rasterio.open(lossyear_url)
                    except Exception as exc:  # noqa: BLE001 - lossyear masking is optional; sampling proceeds without it
                        logger.debug(f"lossyear tile {lat_str}_{lon_str} unavailable: {exc}")

                for idx, lon, lat in tile_pts:
                    try:
                        x, y = to_native.transform(lon, lat) if to_native else (lon, lat)
                        row, col = src.index(x, y)
                        if not (0 <= row < src.height and 0 <= col < src.width):
                            continue
                        raw = float(src.read(1, window=((row, row + 1), (col, col + 1)))[0, 0])
                        if tnd is not None and raw == tnd:
                            continue  # nodata pixel
                        # Mask pixels where forest was lost (lossyear > 0)
                        if loss_src is not None:
                            try:
                                lrow, lcol = loss_src.index(x, y)
                                if 0 <= lrow < loss_src.height and 0 <= lcol < loss_src.width:
                                    lyear = int(loss_src.read(1, window=((lrow, lrow + 1), (lcol, lcol + 1)))[0, 0])
                                    if lyear > 0:
                                        continue  # deforested pixel — label stale
                            except Exception as exc:  # noqa: BLE001 - lossyear masking is optional; point stays unmasked
                                logger.debug(f"lossyear sample at point {idx} failed: {exc}")
                        values[idx] = raw
                    except Exception as exc:  # noqa: BLE001 - one bad point shouldn't fail the whole tile; skipped
                        logger.debug(f"COG tile sample at point {idx} failed: {exc}")

                if loss_src is not None:
                    loss_src.close()

        except Exception as exc:  # noqa: BLE001 - one bad tile shouldn't fail the whole batch; already logged
            logger.warning(f"Tiled COG tile {lat_str}_{lon_str} failed: {exc}")

    valid = sum(1 for v in values if v is not None)
    logger.info(f"COG tiled sampled {len(points)} points → {valid} valid")
    return values


def _tile_key(lat: float, lon: float, tile_size_deg: int = 10) -> tuple[str, str]:
    """
    Return (lat_str, lon_str) tile identifiers for Hansen GFC tile URL naming.

    Hansen tiles are labeled by their northern (lat) and western (lon) edge.
    Example: tile covering lat [-10, 0], lon [110, 120] → ('00N', '110E').
    """
    lat_north = math.ceil(lat / tile_size_deg + 1e-10) * tile_size_deg
    lon_west = math.floor(lon / tile_size_deg) * tile_size_deg
    lat_str = f"{abs(lat_north):02d}{'N' if lat_north >= 0 else 'S'}"
    lon_str = f"{abs(lon_west):03d}{'E' if lon_west >= 0 else 'W'}"
    return (lat_str, lon_str)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _apply_transform(value: float | None, transform: str) -> float | None:
    """Apply registry transform string to a raw pixel value."""
    if value is None:
        return None
    if transform == "divide_10":
        return value / 10.0
    if transform == "multiply_0.47":
        return value * 0.47
    if transform == "multiply_0.94":
        return value * 0.94  # e.g., Hansen treecover% → Mg C/ha proxy (×2.0 ×0.47)
    if transform == "multiply_2.0":
        return value * 2.0
    if transform == "none" or not transform:
        return value
    logger.warning(f"Unknown transform '{transform}', returning raw value")
    return value


def _geojson_to_bbox(geojson: dict) -> tuple[float, float, float, float]:
    """Extract ``(west, south, east, north)`` from any GeoJSON object.

    AOIs from the frontend are commonly wrapped in a ``Feature`` while
    imported/uploaded AOIs may be a bare geometry or a ``FeatureCollection``.
    Walk the GeoJSON structure instead of assuming coordinates live on the
    top-level object; this also keeps multipart and geometry collections safe.
    """

    def iter_coordinates(obj: dict):
        obj_type = obj.get("type", "")
        if obj_type == "Feature":
            geometry = obj.get("geometry")
            if isinstance(geometry, dict):
                yield from iter_coordinates(geometry)
        elif obj_type == "FeatureCollection":
            for feature in obj.get("features", []):
                if isinstance(feature, dict):
                    yield from iter_coordinates(feature)
        elif obj_type == "GeometryCollection":
            for geometry in obj.get("geometries", []):
                if isinstance(geometry, dict):
                    yield from iter_coordinates(geometry)
        else:
            def walk(value):
                if isinstance(value, (list, tuple)):
                    if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
                        yield value
                    else:
                        for child in value:
                            yield from walk(child)

            yield from walk(obj.get("coordinates", []))

    geojson_type = geojson.get("type", "") if isinstance(geojson, dict) else ""
    coords = list(iter_coordinates(geojson)) if isinstance(geojson, dict) else []
    if not coords:
        raise ValueError(f"Cannot extract bbox from GeoJSON type '{geojson_type}'")

    lons = [float(c[0]) for c in coords]
    lats = [float(c[1]) for c in coords]
    return min(lons), min(lats), max(lons), max(lats)
