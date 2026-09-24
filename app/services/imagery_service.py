"""Raw satellite imagery browser - list individual scenes with their real
acquisition date+time and view any single one visualized appropriately for
its sensor type. Sentinel-2 uses a date-range mosaic automatically when one
granule cannot cover the requested AOI; no land-cover/vegetation/carbon
analysis is involved.

User request: "bagaimana bisa melihat citra satelit utk tanggal beserta jam
tertentu, tanpa harus land cover?" - every existing analysis module
(vegetation/carbon/landcover) always builds a MEDIAN/MODE composite over a
date range and only ever exposes month-level (or, for Dynamic World only,
day-level) granularity - none of them expose the actual per-scene
`system:time_start` (which carries a real time-of-day, e.g. Sentinel-2's
~03:15 UTC overpass) or let a user view one single acquisition unmodified.
Later extended (user request) to more than just optical Sentinel-2/Landsat:
Sentinel-1 SAR and Sentinel-5P atmospheric-gas products, each needing a
genuinely different visualization strategy - see imagery_provider_registry.py
for the full rationale on why that's a separate catalog from
satellite_provider_registry.py (vegetation/carbon's index-analysis one).

Unlike the analysis pipelines, exact optical scenes remain available as an
unmasked view (clouds visible as white), while the browser can opt into a
per-pixel mask and the large-AOI mosaic uses that selected mask. SAR is
naturally cloud-independent; S5P products have no per-pixel cloud mask applied
here either (their own `cloud_fraction`/similar bands are metadata, not
something this endpoint filters on beyond the optional `max_cloud_cover`
scene-level query for optical sensors).
"""
from __future__ import annotations

import logging
import hashlib
import re
import socket
import threading
from ipaddress import ip_address
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from importlib.util import find_spec
from datetime import UTC, datetime

import ee
import httpx
import numpy as np
import rasterio
from rasterio.mask import mask as raster_mask
from rasterio.warp import transform_geom as warp_transform_geom
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData

from app.core.config import get_settings
from app.registries.imagery_provider_registry import (
    IMAGERY_PROVIDERS,
    get_imagery_provider_meta,
    provider_capabilities,
    resolve_imagery_provider,
)
from app.services.gee_common import (
    AnalysisError,
    create_geometry_from_payload,
    get_tile_url,
    resolve_cloud_mask_technique,
)
from app.services.commercial_imagery_service import (
    COMMERCIAL_PROVIDER_KEYS,
    configuration as commercial_configuration,
    provider_meta as commercial_provider_meta,
    request_headers as commercial_request_headers,
)
from app.services.geo_utils import bbox_and_centroid

logger = logging.getLogger(__name__)

# Keep a generous safety ceiling for one browsing response. The UI paginates
# this collection, so users are no longer forced to stop at 200 scenes.
_MAX_SCENES = 2000
_SENTINEL2_MOSAIC_PROVIDER_KEYS = {"sentinel2", "sentinel2_l1c"}
_COMMERCIAL_ITEM_CACHE: dict[tuple[str, str], str] = {}
_COMMERCIAL_ITEM_CACHE_LOCK = threading.RLock()
_COPERNICUS_PROVIDER_KEYS = {"copernicus_s2_l2a", "copernicus_s2_l1c"}
_OPENAERIALMAP_PROVIDER_KEY = "openaerialmap"
_MAXAR_OPEN_DATA_PROVIDER_KEY = "vantor_open_data"
_PLANET_OPEN_DATA_PROVIDER_KEY = "planet_open_data"
_GENERIC_STAC_PROVIDER_KEY = "stac_catalog"
_BIG_CTSRT_PROVIDER_KEY = "big_ctsrt"
_BIG_CTSRT_INDEX_MAP_URL = "https://geoservices.big.go.id/rbi/rest/services/INDEKS/CTSRT/MapServer"
_BIG_CTSRT_IMAGERY_FOLDER_URL = "https://geoservices.big.go.id/raster/rest/services/IMAGERY"
_BIG_CTSRT_IMAGERY_ROOT_URL = "https://geoservices.big.go.id/raster/rest/services/IMAGERY"
_BIG_CTSRT_SERVICE_PATTERN = re.compile(r"^CTSRT_(?:19|20)\d{2}_[A-Z0-9_]+$")
_OAM_STAC_SEARCH_URL = "https://api.imagery.hotosm.org/stac/search"
_OAM_TILE_URL_TEMPLATE = (
    "https://api.imagery.hotosm.org/raster/collections/openaerialmap/items/"
    "{item_id}/tiles/WebMercatorQuad/{z}/{x}/{y}?assets=visual&nodata=0"
)
_MAXAR_EVENTS_CATALOG_URL = "https://maxar-opendata.s3.amazonaws.com/events/catalog.json"
_PLANET_OPEN_DATA_CATALOG_URL = "https://data.source.coop/planet/disasterdata/catalog.json"
_SUPER_RESOLUTION_FACTORS = {
    "off": 1,
    "bicubic_2x": 2,
    "bicubic_4x": 4,
}


def _store_commercial_item(provider_key: str, item_url: str) -> str:
    token = hashlib.sha256(f"{provider_key}:{item_url}".encode("utf-8")).hexdigest()[:32]
    public_id = f"commercial:{provider_key}:{token}"
    with _COMMERCIAL_ITEM_CACHE_LOCK:
        _COMMERCIAL_ITEM_CACHE[(provider_key, public_id)] = item_url
    return public_id


def _resolve_commercial_item(provider_key: str, item_url: str) -> str:
    if item_url.startswith("commercial:"):
        with _COMMERCIAL_ITEM_CACHE_LOCK:
            resolved = _COMMERCIAL_ITEM_CACHE.get((provider_key, item_url))
        if not resolved:
            raise AnalysisError("Referensi scene commercial sudah kedaluwarsa; lakukan search ulang", 410)
        return resolved
    return item_url


def _copernicus_geosave_service():
    try:
        from app.services import copernicus_geosave_service
    except ModuleNotFoundError:
        return None
    return copernicus_geosave_service


def _copernicus_geosave_available() -> bool:
    return find_spec("geosave_engine") is not None and _copernicus_geosave_service() is not None


def list_providers() -> dict:
    """GET /imagery/providers - static (no GEE) catalog for the satellite picker."""
    providers = dict(IMAGERY_PROVIDERS)
    copernicus_service = _copernicus_geosave_service()
    if find_spec("geosave_engine") is not None and copernicus_service is not None:
        providers.update(copernicus_service.COPERNICUS_PROVIDERS)
    catalog = {}
    for key, meta in providers.items():
        enriched = {**meta, "capabilities": provider_capabilities(meta)}
        if key in COMMERCIAL_PROVIDER_KEYS:
            enriched.update(commercial_provider_meta(key))
        catalog[key] = enriched
    return {
        "providers": catalog,
        "default": "sentinel2",
    }


def _aoi_payload_to_bbox(aoi_payload: dict) -> list[float]:
    if not isinstance(aoi_payload, dict):
        raise AnalysisError("AOI payload must be an object", 400)
    if aoi_payload.get("type") in {"Feature", "FeatureCollection", "Polygon", "MultiPolygon"}:
        aoi_payload = {"geojson": aoi_payload}
    if "geojson" in aoi_payload:
        bbox, _centroid = bbox_and_centroid(aoi_payload["geojson"])
        if not bbox:
            raise AnalysisError("AOI GeoJSON tidak valid untuk pencarian OpenAerialMap", 400)
        return bbox

    required = {"west", "south", "east", "north"}
    if not required.issubset(aoi_payload.keys()):
        raise AnalysisError("AOI bounds missing west/south/east/north", 400)
    return [
        float(aoi_payload["west"]),
        float(aoi_payload["south"]),
        float(aoi_payload["east"]),
        float(aoi_payload["north"]),
    ]


def _format_stac_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except ValueError:
        return value


def _date_to_stac_boundary(value: str, end_of_day: bool = False) -> str:
    if "T" in value:
        return value
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return f"{value}{suffix}"


def _parse_stac_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace(" ", "T").replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(UTC)
    except ValueError:
        return None


def _bbox_intersects(a: list[float] | None, b: list[float] | None) -> bool:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _extent_intersects_aoi(extent: dict[str, Any] | None, aoi_bbox: list[float]) -> bool:
    boxes = (extent or {}).get("spatial", {}).get("bbox", [])
    return any(_bbox_intersects(box, aoi_bbox) for box in boxes)


def _extent_intersects_date(extent: dict[str, Any] | None, start_dt: datetime, end_dt: datetime) -> bool:
    intervals = (extent or {}).get("temporal", {}).get("interval", [])
    if not intervals:
        return True
    for interval in intervals:
        interval_start = _parse_stac_datetime(interval[0] if interval else None)
        interval_end = _parse_stac_datetime(interval[1] if len(interval) > 1 else None)
        if interval_start and interval_start > end_dt:
            continue
        if interval_end and interval_end < start_dt:
            continue
        return True
    return False


def _abs_stac_href(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def _fetch_json(client: httpx.Client, url: str) -> dict[str, Any]:
    for _ in range(6):
        _validate_public_http_url(url)
        response = client.get(url, follow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise AnalysisError("Redirect STAC tidak memiliki tujuan", 502)
            url = urljoin(url, location)
            continue
        response.raise_for_status()
        return response.json()
    raise AnalysisError("Terlalu banyak redirect STAC", 502)


def _validate_public_http_url(url: str, label: str = "URL") -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AnalysisError(f"{label} harus berupa URL http/https yang valid", 400)

    host = parsed.hostname.strip().lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise AnalysisError(f"{label} tidak boleh mengarah ke host lokal/private", 400)

    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AnalysisError(f"{label} tidak dapat di-resolve: {host}", 400) from exc

    for family, _, _, _, sockaddr in addresses:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        raw_ip = sockaddr[0].split("%", 1)[0]
        try:
            resolved_ip = ip_address(raw_ip)
        except ValueError:
            continue
        if (
            resolved_ip.is_private
            or resolved_ip.is_loopback
            or resolved_ip.is_link_local
            or resolved_ip.is_multicast
            or resolved_ip.is_reserved
            or resolved_ip.is_unspecified
        ):
            raise AnalysisError(f"{label} tidak boleh mengarah ke jaringan lokal/private", 400)


def _stac_datetime_range(data: dict, provider_label: str) -> tuple[datetime, datetime, str]:
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)
    start_dt = _parse_stac_datetime(_date_to_stac_boundary(data["start_date"]))
    end_dt = _parse_stac_datetime(_date_to_stac_boundary(data["end_date"], end_of_day=True))
    if not start_dt or not end_dt:
        raise AnalysisError(f"Rentang tanggal tidak valid untuk {provider_label}", 400)
    interval = f"{_date_to_stac_boundary(data['start_date'])}/{_date_to_stac_boundary(data['end_date'], end_of_day=True)}"
    return start_dt, end_dt, interval


def _is_cog_like_asset(key: str, asset: dict[str, Any]) -> bool:
    href = str(asset.get("href") or "").lower()
    media_type = str(asset.get("type") or "").lower()
    roles = {str(role).lower() for role in asset.get("roles") or []}
    key_l = key.lower()
    if (roles.intersection({"thumbnail", "overview"})
            or any(word in key_l for word in ("thumbnail", "preview", "overview"))
            or urlparse(href).path.endswith((".jpg", ".jpeg", ".png", ".webp"))):
        return False
    return (
        href.endswith((".tif", ".tiff"))
        or "geotiff" in media_type
        or "cog" in media_type
        or key_l in {"visual", "analytic", "analytic_sr", "data", "image", "ortho"}
        or bool(roles.intersection({"visual", "data", "analytic", "reflectance"}))
    )


def _stac_asset_options(item_url: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    options = []
    for key, asset in (item.get("assets") or {}).items():
        if not isinstance(asset, dict) or not asset.get("href") or not _is_cog_like_asset(key, asset):
            continue
        options.append(
            {
                "key": key,
                "title": asset.get("title") or key,
                "href": _abs_stac_href(item_url, asset["href"]),
                "type": asset.get("type"),
                "roles": asset.get("roles") or [],
                "resolution_m": _asset_resolution_m(asset),
            }
        )
    return options


def _default_stac_asset_key(assets: list[dict[str, Any]]) -> str | None:
    if not assets:
        return None
    by_key = {asset["key"]: asset for asset in assets}
    for key in ("visual", "ortho", "analytic", "analytic_sr", "data"):
        if key in by_key:
            return key
    return assets[0]["key"]


def _stac_item_url(default_url: str, item: dict[str, Any]) -> str:
    for link in item.get("links", []):
        if link.get("rel") in {"self", "canonical"} and link.get("href"):
            return _abs_stac_href(default_url, link["href"])
    return default_url


def _scene_from_stac_item(
    item_url: str,
    item: dict[str, Any],
    meta: dict[str, Any],
    producer: str | None,
    request=None,
) -> dict[str, Any] | None:
    scene_url = _stac_item_url(item_url, item)
    assets = _stac_asset_options(scene_url, item)
    default_asset_key = _default_stac_asset_key(assets)
    if not default_asset_key:
        return None
    props = item.get("properties") or {}
    item_dt = _parse_stac_datetime(props.get("datetime") or props.get("start_datetime") or props.get("end_datetime"))
    cloud = props.get(meta.get("cloud_property") or "eo:cloud_cover")
    default_asset = next((asset for asset in assets if asset["key"] == default_asset_key), assets[0])
    base = str(request.base_url).rstrip("/") if request is not None else ""
    is_commercial = meta.get("key") in COMMERCIAL_PROVIDER_KEYS
    public_scene_id = _store_commercial_item(str(meta["key"]), scene_url) if is_commercial else scene_url
    if base and is_commercial:
        download_url = (
            f"{base}/api/imagery/commercial-source?provider_key={quote(str(meta['key']), safe='')}"
            f"&item_url={quote(public_scene_id, safe='')}&asset_key={quote(default_asset_key, safe='')}"
        )
    else:
        download_url = (
            f"{base}/api/imagery/stac-source?item_url={quote(scene_url, safe='')}&asset_key={quote(default_asset_key, safe='')}"
            if base
            else default_asset["href"]
        )
    return {
        "id": public_scene_id,
        "acquired_at": (item_dt.isoformat().replace("+00:00", "Z") if item_dt else ""),
        "cloud_cover_pct": round(float(cloud), 1) if cloud is not None else None,
        "bbox": item.get("bbox"),
        "footprint": item.get("geometry"),
        "resolution_m": props.get("gsd") or default_asset.get("resolution_m") or meta.get("resolution_m"),
        "platform": props.get("platform") or props.get("constellation"),
        "producer": producer or meta.get("provider"),
        "title": props.get("title") or props.get("catalog_id") or item.get("id"),
        "assets": [
            {
                **asset,
                "href": (
                    f"{base}/api/imagery/commercial-source?provider_key={quote(str(meta['key']), safe='')}"
                    f"&item_url={quote(public_scene_id, safe='')}&asset_key={quote(asset['key'], safe='')}"
                    if base and is_commercial
                    else asset["href"]
                ),
            }
            for asset in assets
        ],
        "default_asset_key": default_asset_key,
        "download_url": download_url,
    }


def _stac_api_search(
    client: httpx.Client,
    search_url: str,
    data: dict,
    meta: dict[str, Any],
    aoi_bbox: list[float],
    interval: str,
    producer: str | None,
    request=None,
) -> tuple[list[dict[str, Any]], bool | None]:
    body: dict[str, Any] = {"bbox": aoi_bbox, "datetime": interval, "limit": _MAX_SCENES}
    collections = data.get("stac_collections") or data.get("stac_collection")
    if isinstance(collections, str) and collections.strip():
        body["collections"] = [part.strip() for part in collections.split(",") if part.strip()]
    elif isinstance(collections, list) and collections:
        body["collections"] = collections

    scenes = []
    seen = set()
    method = "POST"
    url = search_url
    for _ in range(20):
        _validate_public_http_url(url)
        if method == "GET":
            response = client.get(url)
        else:
            response = client.post(url, json=body)
            if response.status_code in {405, 501}:
                params = {key: ",".join(map(str, value)) if isinstance(value, list) else value for key, value in body.items()}
                response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        for feature in payload.get("features", []):
            scene_url = _stac_item_url(url, feature)
            if scene_url in seen:
                continue
            seen.add(scene_url)
            cloud = (feature.get("properties") or {}).get("eo:cloud_cover")
            if data.get("max_cloud_cover") is not None and cloud is not None and float(cloud) > float(data["max_cloud_cover"]):
                continue
            scene = _scene_from_stac_item(scene_url, feature, meta, producer, request)
            if scene:
                scenes.append(scene)
            if len(scenes) >= _MAX_SCENES:
                return scenes, True
        next_link = next((link for link in payload.get("links", []) if link.get("rel") == "next"), None)
        if not next_link:
            return scenes, False
        url = _abs_stac_href(url, next_link["href"])
        method = next_link.get("method", "GET").upper()
        body = {**body, **next_link.get("body", {})} if next_link.get("merge") else next_link.get("body", {})
    return scenes, True


def _list_static_stac_scenes(
    client: httpx.Client,
    catalog_url: str,
    data: dict,
    meta: dict[str, Any],
    aoi_bbox: list[float],
    start_dt: datetime,
    end_dt: datetime,
    producer: str | None,
    request=None,
) -> tuple[list[dict[str, Any]], bool]:
    max_cloud_cover = data.get("max_cloud_cover")
    scenes: list[dict[str, Any]] = []
    queue = [catalog_url]
    visited: set[str] = set()
    truncated = False

    while queue and len(scenes) < _MAX_SCENES and len(visited) < 1000:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        node = _fetch_json(client, url)
        node_type = node.get("type")

        if node_type == "FeatureCollection":
            for feature in node.get("features", []):
                scene_url = _stac_item_url(url, feature)
                props = feature.get("properties") or {}
                item_dt = _parse_stac_datetime(
                    props.get("datetime") or props.get("start_datetime") or props.get("end_datetime")
                )
                if item_dt and (item_dt < start_dt or item_dt > end_dt):
                    continue
                if not _bbox_intersects(feature.get("bbox"), aoi_bbox):
                    continue
                cloud = props.get(meta.get("cloud_property") or "eo:cloud_cover")
                if max_cloud_cover is not None and cloud is not None and float(cloud) > float(max_cloud_cover):
                    continue
                scene = _scene_from_stac_item(scene_url, feature, meta, producer, request)
                if scene:
                    scenes.append(scene)
                if len(scenes) >= _MAX_SCENES:
                    break
            continue

        if node_type == "Feature":
            props = node.get("properties") or {}
            item_dt = _parse_stac_datetime(props.get("datetime") or props.get("start_datetime") or props.get("end_datetime"))
            if item_dt and (item_dt < start_dt or item_dt > end_dt):
                continue
            if not _bbox_intersects(node.get("bbox"), aoi_bbox):
                continue
            cloud = props.get(meta.get("cloud_property") or "eo:cloud_cover")
            if max_cloud_cover is not None and cloud is not None and float(cloud) > float(max_cloud_cover):
                continue
            scene = _scene_from_stac_item(url, node, meta, producer, request)
            if scene:
                scenes.append(scene)
            continue

        extent = node.get("extent")
        if extent:
            if not _extent_intersects_aoi(extent, aoi_bbox):
                continue
            if not _extent_intersects_date(extent, start_dt, end_dt):
                continue

        for link in node.get("links", []):
            rel = link.get("rel")
            href = link.get("href")
            if not href or rel not in {"child", "item", "items"}:
                continue
            if str(href).lower().endswith((".parquet", ".geojson", ".fgb")):
                continue
            absolute = _abs_stac_href(url, href)
            if absolute in visited:
                continue
            queue.append(absolute)

    if queue:
        truncated = True
    return scenes, truncated


def _find_stac_search_url(root_url: str, root: dict[str, Any]) -> str | None:
    for link in root.get("links", []):
        if link.get("rel") == "search" and link.get("href"):
            return _abs_stac_href(root_url, link["href"])
    return None


def _list_cog_stac_scenes(
    data: dict,
    provider_key: str,
    catalog_url: str,
    producer: str | None = None,
    request=None,
    headers: dict[str, str] | None = None,
) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not catalog_url.startswith(("http://", "https://")):
        raise AnalysisError("URL STAC harus diawali http:// atau https://", 400)

    meta = get_imagery_provider_meta(provider_key)
    aoi_bbox = _aoi_payload_to_bbox(data["aoi"])
    start_dt, end_dt, interval = _stac_datetime_range(data, meta["name"])

    try:
        timeout = max(5, get_settings().commercial_imagery_timeout_seconds) if provider_key in COMMERCIAL_PROVIDER_KEYS else 30
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers or {}) as client:
            root = _fetch_json(client, catalog_url)
            search_url = data.get("stac_search_url") or _find_stac_search_url(catalog_url, root)
            if search_url:
                scenes, api_truncated = _stac_api_search(client, search_url, data, meta, aoi_bbox, interval, producer, request)
                truncated = api_truncated if api_truncated is not None else len(scenes) >= _MAX_SCENES
            else:
                scenes, truncated = _list_static_stac_scenes(
                    client, catalog_url, data, meta, aoi_bbox, start_dt, end_dt, producer, request
                )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500].strip()
        suffix = f" - {detail}" if detail else ""
        raise AnalysisError(f"{meta['name']} STAC error: HTTP {exc.response.status_code}{suffix}", 502)
    except httpx.HTTPError as exc:
        raise AnalysisError(f"Gagal menghubungi {meta['name']}: {exc}", 502)
    except ValueError as exc:
        raise AnalysisError(f"Respons {meta['name']} tidak valid: {exc}", 502)

    scenes.sort(key=lambda scene: scene.get("acquired_at") or "", reverse=True)
    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": meta,
        "truncated": truncated,
    }


def _list_openaerialmap_scenes(data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)

    meta = get_imagery_provider_meta(_OPENAERIALMAP_PROVIDER_KEY)
    bbox = _aoi_payload_to_bbox(data["aoi"])
    body = {
        "collections": ["openaerialmap"],
        "bbox": bbox,
        "datetime": f"{_date_to_stac_boundary(data['start_date'])}/{_date_to_stac_boundary(data['end_date'], end_of_day=True)}",
        "limit": _MAX_SCENES,
    }

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(_OAM_STAC_SEARCH_URL, json=body)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500].strip()
        suffix = f" - {detail}" if detail else ""
        raise AnalysisError(f"OpenAerialMap menolak pencarian STAC: HTTP {exc.response.status_code}{suffix}", 502)
    except httpx.HTTPError as exc:
        raise AnalysisError(f"Gagal menghubungi OpenAerialMap STAC: {exc}", 502)
    except ValueError as exc:
        raise AnalysisError(f"Respons OpenAerialMap STAC tidak valid: {exc}", 502)

    scenes = []
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        scene_id = feature.get("id")
        if not scene_id:
            continue
        acquired_at = (
            _format_stac_datetime(props.get("datetime"))
            or _format_stac_datetime(props.get("start_datetime"))
            or _format_stac_datetime(props.get("end_datetime"))
            or ""
        )
        scenes.append(
            {
                "id": scene_id,
                "acquired_at": acquired_at,
                "cloud_cover_pct": None,
                "bbox": feature.get("bbox"),
                "resolution_m": props.get("gsd"),
                "platform": props.get("oam:platform_type"),
                "producer": props.get("oam:producer_name"),
                "title": props.get("title"),
            }
        )
    scenes.sort(key=lambda scene: scene.get("acquired_at") or "", reverse=True)

    matched = payload.get("context", {}).get("matched")
    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": meta,
        "truncated": len(scenes) >= _MAX_SCENES if matched is None else matched > len(scenes),
    }


def _list_maxar_open_data_scenes(data: dict, request=None) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)

    meta = get_imagery_provider_meta(_MAXAR_OPEN_DATA_PROVIDER_KEY)
    aoi_bbox = _aoi_payload_to_bbox(data["aoi"])
    start_dt = _parse_stac_datetime(_date_to_stac_boundary(data["start_date"]))
    end_dt = _parse_stac_datetime(_date_to_stac_boundary(data["end_date"], end_of_day=True))
    if not start_dt or not end_dt:
        raise AnalysisError("Rentang tanggal tidak valid untuk Vantor/Maxar Open Data", 400)
    max_cloud_cover = data.get("max_cloud_cover")

    try:
        scenes = []
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            root = _fetch_json(client, _MAXAR_EVENTS_CATALOG_URL)
            for event_link in [link for link in root.get("links", []) if link.get("rel") == "child"]:
                event_url = _abs_stac_href(_MAXAR_EVENTS_CATALOG_URL, event_link["href"])
                event = _fetch_json(client, event_url)
                if not _extent_intersects_aoi(event.get("extent"), aoi_bbox):
                    continue
                if not _extent_intersects_date(event.get("extent"), start_dt, end_dt):
                    continue

                for acquisition_link in [link for link in event.get("links", []) if link.get("rel") == "child"]:
                    acquisition_url = _abs_stac_href(event_url, acquisition_link["href"])
                    acquisition = _fetch_json(client, acquisition_url)
                    if not _extent_intersects_aoi(acquisition.get("extent"), aoi_bbox):
                        continue
                    if not _extent_intersects_date(acquisition.get("extent"), start_dt, end_dt):
                        continue

                    for item_link in [link for link in acquisition.get("links", []) if link.get("rel") == "item"]:
                        item_url = _abs_stac_href(acquisition_url, item_link["href"])
                        item = _fetch_json(client, item_url)
                        props = item.get("properties") or {}
                        item_dt = _parse_stac_datetime(props.get("datetime"))
                        if item_dt and (item_dt < start_dt or item_dt > end_dt):
                            continue
                        if not _bbox_intersects(item.get("bbox"), aoi_bbox):
                            continue
                        cloud = props.get("tile:clouds_percent")
                        if max_cloud_cover is not None and cloud is not None and float(cloud) > float(max_cloud_cover):
                            continue
                        visual = (item.get("assets") or {}).get("visual")
                        if not visual or not visual.get("href"):
                            continue
                        scene = _scene_from_stac_item(item_url, item, meta, "Vantor/Maxar Open Data", request)
                        if not scene:
                            continue
                        scene.update(
                            {
                                "cloud_cover_pct": round(float(cloud), 1) if cloud is not None else None,
                                "resolution_m": props.get("gsd") or _asset_resolution_m(visual),
                                "title": f"{event.get('title') or event.get('id')} - {props.get('catalog_id') or item.get('id')}",
                            }
                        )
                        scenes.append(scene)
                        if len(scenes) >= _MAX_SCENES:
                            break
                    if len(scenes) >= _MAX_SCENES:
                        break
                if len(scenes) >= _MAX_SCENES:
                    break
    except httpx.HTTPStatusError as exc:
        raise AnalysisError(f"Vantor/Maxar Open Data STAC error: HTTP {exc.response.status_code}", 502)
    except httpx.HTTPError as exc:
        raise AnalysisError(f"Gagal menghubungi Vantor/Maxar Open Data: {exc}", 502)
    except ValueError as exc:
        raise AnalysisError(f"Respons Vantor/Maxar Open Data tidak valid: {exc}", 502)

    scenes.sort(key=lambda scene: scene.get("acquired_at") or "", reverse=True)
    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": meta,
        "truncated": len(scenes) >= _MAX_SCENES,
    }


def _asset_resolution_m(asset: dict[str, Any]) -> float | None:
    transform = asset.get("proj:transform")
    if isinstance(transform, list) and transform:
        try:
            return abs(float(transform[0]))
        except (TypeError, ValueError):
            return None
    return None


@lru_cache(maxsize=1)
def _big_ctsrt_service_names() -> tuple[str, ...]:
    """Discover the currently published CTSRT ImageServer mosaics."""
    try:
        with httpx.Client(timeout=45, follow_redirects=True) as client:
            response = client.get(_BIG_CTSRT_IMAGERY_FOLDER_URL)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise AnalysisError(f"Gagal membaca katalog service BIG/CTSRT: {exc}", 502) from exc

    names = {
        match.group(1)
        for match in re.finditer(r"/IMAGERY/(CTSRT_[A-Za-z0-9_]+)/ImageServer", response.text)
        if _BIG_CTSRT_SERVICE_PATTERN.fullmatch(match.group(1).upper())
    }
    if not names:
        raise AnalysisError("Katalog service BIG/CTSRT tidak mengembalikan mosaic CTSRT", 502)
    return tuple(sorted(names))


@lru_cache(maxsize=256)
def _big_ctsrt_service_meta(service_name: str) -> dict[str, Any]:
    if not _BIG_CTSRT_SERVICE_PATTERN.fullmatch(service_name.upper()):
        raise AnalysisError("Nama service BIG/CTSRT tidak valid", 400)
    url = f"{_BIG_CTSRT_IMAGERY_ROOT_URL}/{quote(service_name, safe='')}/ImageServer"
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(url, params={"f": "json"})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise AnalysisError(f"Gagal membaca metadata {service_name}: {exc}", 502) from exc
    except ValueError as exc:
        raise AnalysisError(f"Metadata {service_name} tidak valid", 502) from exc
    if payload.get("error"):
        raise AnalysisError(f"BIG/CTSRT service error: {payload['error']}", 502)
    return payload


def _list_big_ctsrt_scenes(data: dict) -> dict:
    """List actual BIG CTSRT ImageServer mosaics intersecting the AOI/year."""
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)

    meta = get_imagery_provider_meta(_BIG_CTSRT_PROVIDER_KEY)
    bbox = _aoi_payload_to_bbox(data["aoi"])
    try:
        start_year = int(str(data["start_date"])[:4])
        end_year = int(str(data["end_date"])[:4])
    except (TypeError, ValueError) as exc:
        raise AnalysisError("Rentang tanggal tidak valid untuk BIG/CTSRT", 400) from exc

    scenes = []
    for service_name in _big_ctsrt_service_names():
        year_match = re.match(r"CTSRT_(\d{4})_", service_name, re.IGNORECASE)
        year = int(year_match.group(1)) if year_match else None
        if year is None or not (start_year <= year <= end_year):
            continue

        service_meta = _big_ctsrt_service_meta(service_name)
        extent = service_meta.get("fullExtent") or service_meta.get("extent") or {}
        bbox_value = [extent.get("xmin"), extent.get("ymin"), extent.get("xmax"), extent.get("ymax")]
        if not all(isinstance(value, (int, float)) for value in bbox_value):
            continue
        if not _bbox_intersects(bbox_value, bbox):
            continue

        service_url = f"{_BIG_CTSRT_IMAGERY_ROOT_URL}/{quote(service_name, safe='')}/ImageServer"
        pixel_size = service_meta.get("pixelSizeX") or service_meta.get("pixelSizeY")
        resolution_m = float(pixel_size) * 111_320 if isinstance(pixel_size, (int, float)) else 0.5
        title = service_meta.get("description") or service_name.replace("_", " ")
        scenes.append(
            {
                "id": f"big-ctsrt:{service_name}",
                "acquired_at": f"{year:04d}-01-01T00:00:00Z",
                "cloud_cover_pct": None,
                "bbox": bbox_value,
                "resolution_m": round(max(resolution_m, 0.1), 2),
                "platform": "CTSRT",
                "producer": "BIG",
                "title": str(title),
                "download_url": service_url,
            }
        )
        if len(scenes) >= _MAX_SCENES:
            break

    scenes.sort(key=lambda scene: scene.get("acquired_at") or "", reverse=True)
    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": meta,
        "truncated": len(scenes) >= _MAX_SCENES,
    }


def _big_ctsrt_tile_bbox(z: int, x: int, y: int) -> str:
    origin = 20037508.342789244
    tile_size = (origin * 2) / (256 * (2 ** z))
    xmin = -origin + x * 256 * tile_size
    xmax = xmin + 256 * tile_size
    ymax = origin - y * 256 * tile_size
    ymin = ymax - 256 * tile_size
    return f"{xmin},{ymin},{xmax},{ymax}"


def render_big_ctsrt_tile(z: int, x: int, y: int, service_name: str | None = None) -> bytes | None:
    """Render one XYZ tile from a BIG CTSRT ImageServer mosaic."""
    if service_name:
        if not _BIG_CTSRT_SERVICE_PATTERN.fullmatch(service_name.upper()):
            raise AnalysisError("Nama service BIG/CTSRT tidak valid", 400)
        export_url = f"{_BIG_CTSRT_IMAGERY_ROOT_URL}/{quote(service_name, safe='')}/ImageServer/exportImage"
    else:
        export_url = f"{_BIG_CTSRT_INDEX_MAP_URL}/export"
    params = {
        "bbox": _big_ctsrt_tile_bbox(z, x, y),
        "bboxSR": "3857",
        "size": "256,256",
        "imageSR": "3857",
        "format": "png32",
        "transparent": "true",
        "f": "image",
    }
    if service_name:
        params["renderingRule"] = '{"rasterFunction":"Stretch","rasterFunctionArguments":{"StretchType":3,"Min":0,"Max":3000,"Gamma":1}}'
    else:
        params["layers"] = "show:0"
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            response = client.get(export_url, params=params)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            return response.content if content_type.startswith("image/") else None
    except httpx.HTTPError as exc:
        logger.warning("BIG/CTSRT tile request failed z=%s x=%s y=%s: %s", z, x, y, exc)
        return None


def list_scenes(data: dict, request=None) -> dict:
    if data.get("satellite") == _BIG_CTSRT_PROVIDER_KEY:
        return _list_big_ctsrt_scenes(data)

    if data.get("satellite") == _OPENAERIALMAP_PROVIDER_KEY:
        return _list_openaerialmap_scenes(data)

    if data.get("satellite") == _MAXAR_OPEN_DATA_PROVIDER_KEY:
        return _list_maxar_open_data_scenes(data, request)

    if data.get("satellite") == _PLANET_OPEN_DATA_PROVIDER_KEY:
        return _list_cog_stac_scenes(
            data,
            _PLANET_OPEN_DATA_PROVIDER_KEY,
            _PLANET_OPEN_DATA_CATALOG_URL,
            "Planet Open Data",
            request,
        )

    if data.get("satellite") == _GENERIC_STAC_PROVIDER_KEY:
        catalog_url = str(data.get("stac_catalog_url") or "").strip()
        if not catalog_url:
            raise AnalysisError("Masukkan URL STAC Catalog/API untuk Generic STAC Catalog Browser", 400)
        return _list_cog_stac_scenes(data, _GENERIC_STAC_PROVIDER_KEY, catalog_url, "Custom STAC", request)

    if data.get("satellite") in COMMERCIAL_PROVIDER_KEYS:
        provider_key = str(data["satellite"])
        config = commercial_configuration(provider_key)
        if not config["configured"]:
            raise AnalysisError(
                f"{config['label']} belum dikonfigurasi. Isi URL STAC dan credential di backend/.env.",
                503,
            )
        return _list_cog_stac_scenes(
            data,
            provider_key,
            config["catalog_url"],
            config["label"],
            request,
            headers=commercial_request_headers(provider_key),
        )

    if data.get("satellite") in _COPERNICUS_PROVIDER_KEYS:
        if not _copernicus_geosave_available():
            raise AnalysisError(
                "Provider Copernicus CDSE membutuhkan paket opsional geosave_engine. "
                "Install paket tersebut atau gunakan provider GEE/Esri Wayback.",
                503,
            )
        return _copernicus_geosave_service().list_scenes(data)

    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise AnalysisError("start_date and end_date are required", 400)

    aoi = create_geometry_from_payload(data["aoi"])
    provider_key = resolve_imagery_provider(data.get("satellite"))
    meta = get_imagery_provider_meta(provider_key)

    start_date = data["start_date"]
    end_date = data["end_date"]
    max_cloud_cover = data.get("max_cloud_cover")
    cloud_prop = meta.get("cloud_property")

    col = (
        ee.ImageCollection(meta["gee_collection"])
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .sort("system:time_start")
    )
    if cloud_prop and max_cloud_cover is not None:
        col = col.filter(ee.Filter.lte(cloud_prop, float(max_cloud_cover)))
    col = col.limit(_MAX_SCENES)

    total_size = col.size().getInfo()
    if total_size == 0:
        return {"scenes": [], "count": 0, "satellite": meta, "truncated": False}

    # Single round-trip: bundle all per-scene arrays into one server-side
    # dict so this is one getInfo() call, not one per scene.
    to_aggregate = {"ids": col.aggregate_array("system:index"), "times": col.aggregate_array("system:time_start")}
    if cloud_prop:
        to_aggregate["clouds"] = col.aggregate_array(cloud_prop)
    bundle = ee.Dictionary(to_aggregate).getInfo()

    ids = bundle["ids"]
    times = bundle["times"]
    clouds = bundle.get("clouds") if cloud_prop else [None] * len(ids)

    scenes = []
    for scene_id, ts, cloud in zip(ids, times, clouds):
        acquired = datetime.fromtimestamp(ts / 1000, tz=UTC)
        scenes.append(
            {
                "id": scene_id,
                # Real acquisition timestamp incl. time-of-day (UTC) - this is
                # the whole point of this endpoint vs. every composite-based one.
                "acquired_at": acquired.isoformat().replace("+00:00", "Z"),
                "resolution_m": meta.get("resolution_m"),
                "cloud_cover_pct": round(float(cloud), 1) if cloud is not None else None,
            }
        )

    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": meta,
        "truncated": total_size >= _MAX_SCENES,
    }


def _apply_s2_single_scene_mask(img: ee.Image, technique: str) -> ee.Image:
    """Per-pixel cloud mask for ONE Sentinel-2 scene (not a collection
    composite) - user request: let the scene browser optionally mask clouds
    using the same SCL/QA60/s2cloudless techniques already available for
    Vegetation/Carbon (gee_common.build_s2_cloud_masked_collection), instead
    of only ever showing the raw unmasked scene. Kept separate from that
    function since these operate on a single ee.Image, not an
    ee.ImageCollection - the underlying per-technique logic is the same.

    "scl" is only valid for the L2A (Surface Reflectance) provider - the L1C/
    TOA variant has no SCL band at all (verified live) - callers must not
    offer "scl" for that provider (see IMAGERY_PROVIDERS[...]["cloud_mask_techniques"]).
    """
    if technique == "qa60":
        qa = img.select("QA60")
        cloud = qa.bitwiseAnd(1 << 10).neq(0)
        cirrus = qa.bitwiseAnd(1 << 11).neq(0)
        return img.updateMask(cloud.Or(cirrus).Not())
    if technique == "s2cloudless":
        prob_img = (
            ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY")
            .filter(ee.Filter.eq("system:index", img.get("system:index")))
            .first()
        )
        prob = ee.Image(prob_img).select("probability")
        return img.updateMask(prob.lt(40))
    # "scl" (default)
    scl = img.select("SCL")
    mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    return img.updateMask(mask)


def _aoi_exceeds_scene_footprint(aoi: ee.Geometry, scene: ee.Image) -> bool:
    """Return whether the requested AOI is larger than one Sentinel-2 granule.

    Sentinel-2 tiles are roughly 100 km wide, while users often draw an AOI
    spanning an entire province.  Keep this check server-side and bundle both
    area requests into one ``getInfo`` call.  If Earth Engine cannot evaluate
    the footprint for an unusual image, retain the safe single-scene behavior.
    """
    try:
        areas = ee.Dictionary({
            "aoi": aoi.area(maxError=1),
            # The Python Earth Engine Image wrapper does not expose the
            # JavaScript ``image.geometry()`` helper.  Sentinel-2 publishes
            # the equivalent footprint as the system:footprint property.
            "scene": ee.Geometry(scene.get("system:footprint")).area(maxError=1),
        }).getInfo()
        aoi_area = float((areas or {}).get("aoi") or 0)
        scene_area = float((areas or {}).get("scene") or 0)
        return aoi_area > 0 and scene_area > 0 and aoi_area > scene_area
    except Exception:  # noqa: BLE001
        logger.warning("Could not compare AOI and Sentinel-2 scene footprint; using single scene")
        return False


def _get_sentinel2_mosaic_tile(data: dict, aoi: ee.Geometry, meta: dict) -> dict | None:
    """Build one cloud-masked RGB mosaic for a Sentinel-2 date range.

    The scene browser still returns and identifies exact acquisitions. This
    helper is used automatically when the selected granule cannot cover the
    requested AOI, or explicitly when the user selects the mosaic group, so
    the map can fill the AOI from all matching granules without changing the
    scene table semantics.
    """
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    if not start_date or not end_date:
        return None

    collection = (
        ee.ImageCollection(meta["gee_collection"])
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .sort("system:time_start")
    )
    cloud_prop = meta.get("cloud_property")
    max_cloud_cover = data.get("max_cloud_cover")
    if cloud_prop and max_cloud_cover is not None:
        collection = collection.filter(ee.Filter.lte(cloud_prop, float(max_cloud_cover)))
    collection = collection.limit(_MAX_SCENES)
    scene_count = int(collection.size().getInfo() or 0)
    if scene_count == 0:
        return None

    available_techniques = meta.get("cloud_mask_techniques") or []
    requested_technique = data.get("cloud_mask_technique")
    applied_cloud_mask = None
    if available_techniques and requested_technique:
        technique = resolve_cloud_mask_technique(requested_technique)
        if technique not in available_techniques:
            technique = available_techniques[0]
        applied_cloud_mask = technique

    band_role_map = meta["band_role_map"]
    bands = list(band_role_map.values())

    def prepare_image(image):
        if applied_cloud_mask:
            image = _apply_s2_single_scene_mask(image, applied_cloud_mask)
        return image.select(bands).multiply(meta["reflectance_scale"]).add(meta.get("reflectance_offset", 0))

    mosaic = collection.map(prepare_image).mosaic().clip(aoi)
    vis = {
        "bands": [band_role_map["red"], band_role_map["green"], band_role_map["blue"]],
        "min": meta["vis_min"],
        "max": meta["vis_max"],
    }
    mosaic, super_resolution = _apply_super_resolution(mosaic, meta, data.get("super_resolution"))
    tile = get_tile_url(mosaic, vis, f"{meta['name']} mosaic")
    if not tile:
        raise AnalysisError("Gagal membuat tile mosaik Sentinel-2", 500)
    return {
        "tile_url": tile["tile_url"],
        "scene_count": scene_count,
        "super_resolution": super_resolution,
        "cloud_mask_technique": applied_cloud_mask,
    }


def _apply_super_resolution(img: ee.Image, meta: dict, mode: str | None) -> tuple[ee.Image, dict | None]:
    """Visual super-resolution for GEE-backed scene tiles.

    This is intentionally an image-resampling enhancement (bicubic), not an AI
    hallucination/detail-recovery model. It improves tile smoothness at high
    zoom while preserving the original scene values and provenance.
    """
    factor = _SUPER_RESOLUTION_FACTORS.get(str(mode or "off"), 1)
    if factor <= 1:
        return img, None

    native_scale = float(meta.get("resolution_m") or 10)
    # Optional display interpolation only. Do not invent a finer native grid.
    return img.resample("bicubic"), {
        "mode": str(mode),
        # The native grid is unchanged; keep the reported factor at one so
        # downstream consumers never interpret display interpolation as new
        # source resolution. Preserve the requested preset separately.
        "factor": 1,
        "requested_factor": factor,
        "native_resolution_m": native_scale,
        "render_scale_m": native_scale,
        "method": "Bicubic display interpolation; no added source detail",
        "enhancement_backend": "gee_bicubic",
        "is_visual_only": True,
    }



def get_scene_tile(data: dict, request=None) -> dict:
    if data.get("satellite") == _BIG_CTSRT_PROVIDER_KEY:
        if not data.get("scene_id"):
            raise AnalysisError("scene_id is required", 400)
        if request is None:
            raise AnalysisError("request context is required for BIG/CTSRT tile URLs", 500)
        meta = get_imagery_provider_meta(_BIG_CTSRT_PROVIDER_KEY)
        service_name = str(data["scene_id"]).removeprefix("big-ctsrt:")
        if not _BIG_CTSRT_SERVICE_PATTERN.fullmatch(service_name.upper()):
            raise AnalysisError("Scene BIG/CTSRT tidak valid", 400)
        base = str(request.base_url).rstrip("/")
        return {
            "scene_id": data["scene_id"],
            "tile_url": f"{base}/api/imagery/big-ctsrt-tiles/{{z}}/{{x}}/{{y}}.png?service={quote(service_name, safe='')}",
            "satellite": meta,
            "super_resolution": None,
        }

    if data.get("satellite") == _OPENAERIALMAP_PROVIDER_KEY:
        if not data.get("scene_id"):
            raise AnalysisError("scene_id is required", 400)
        meta = get_imagery_provider_meta(_OPENAERIALMAP_PROVIDER_KEY)
        scene_id = data["scene_id"]
        return {
            "scene_id": scene_id,
            "tile_url": _OAM_TILE_URL_TEMPLATE.format(item_id=quote(scene_id, safe=""), z="{z}", x="{x}", y="{y}"),
            "satellite": meta,
            "super_resolution": None,
        }

    if data.get("satellite") == _MAXAR_OPEN_DATA_PROVIDER_KEY:
        if not data.get("scene_id"):
            raise AnalysisError("scene_id is required", 400)
        if request is None:
            raise AnalysisError("request context is required for Vantor/Maxar tile URLs", 500)
        meta = get_imagery_provider_meta(_MAXAR_OPEN_DATA_PROVIDER_KEY)
        scene_id = data["scene_id"]
        base = str(request.base_url).rstrip("/")
        query = _cog_tile_query(data, scene_id, default_asset_key="visual")
        tile_url = (
            f"{base}/api/imagery/maxar-open-data-tiles/{{z}}/{{x}}/{{y}}.png"
            f"?{query}"
        )
        return {
            "scene_id": scene_id,
            "tile_url": tile_url,
            "satellite": meta,
            "super_resolution": None,
        }

    if data.get("satellite") in {_PLANET_OPEN_DATA_PROVIDER_KEY, _GENERIC_STAC_PROVIDER_KEY}:
        if not data.get("scene_id"):
            raise AnalysisError("scene_id is required", 400)
        if request is None:
            raise AnalysisError("request context is required for STAC COG tile URLs", 500)
        provider_key = resolve_imagery_provider(data.get("satellite"))
        meta = get_imagery_provider_meta(provider_key)
        scene_id = data["scene_id"]
        base = str(request.base_url).rstrip("/")
        tile_url = (
            f"{base}/api/imagery/stac-cog-tiles/{{z}}/{{x}}/{{y}}.png"
            f"?{_cog_tile_query(data, scene_id)}"
        )
        return {
            "scene_id": scene_id,
            "tile_url": tile_url,
            "satellite": meta,
            "super_resolution": None,
        }

    if data.get("satellite") in COMMERCIAL_PROVIDER_KEYS:
        provider_key = str(data["satellite"])
        if not commercial_configuration(provider_key)["configured"]:
            raise AnalysisError(f"{provider_key} belum dikonfigurasi di backend", 503)
        if not data.get("scene_id"):
            raise AnalysisError("scene_id is required", 400)
        if request is None:
            raise AnalysisError("request context is required for commercial STAC tile URLs", 500)
        base = str(request.base_url).rstrip("/")
        query = _cog_tile_query(data, str(data["scene_id"]))
        return {
            "scene_id": data["scene_id"],
            "tile_url": f"{base}/api/imagery/commercial-tiles/{provider_key}/{{z}}/{{x}}/{{y}}.png?{query}",
            "satellite": get_imagery_provider_meta(provider_key),
            "super_resolution": None,
        }

    if data.get("satellite") in _COPERNICUS_PROVIDER_KEYS:
        if not _copernicus_geosave_available():
            raise AnalysisError(
                "Provider Copernicus CDSE membutuhkan paket opsional geosave_engine. "
                "Install paket tersebut atau gunakan provider GEE/Esri Wayback.",
                503,
            )
        if request is None:
            raise AnalysisError("request context is required for Copernicus tile URLs", 500)
        return _copernicus_geosave_service().get_scene_tile(data, request)

    if not data.get("scene_id"):
        raise AnalysisError("scene_id is required", 400)

    provider_key = resolve_imagery_provider(data.get("satellite"))
    meta = get_imagery_provider_meta(provider_key)
    scene_id = data["scene_id"]

    matches = ee.ImageCollection(meta["gee_collection"]).filter(ee.Filter.eq("system:index", scene_id))
    # ee.ImageCollection.first() on a filter that matches nothing evaluates to
    # a server-side null, not a band-less-but-valid Image - calling anything
    # on it raises an opaque EEException only once GEE evaluates it. Check
    # the FILTERED COLLECTION's size first instead, so a bad/stale scene_id
    # is a clean 404 (verified live against this exact failure mode).
    if matches.size().getInfo() == 0:
        raise AnalysisError(f"Scene '{scene_id}' tidak ditemukan untuk {meta['name']}", 404)
    img = matches.first()
    aoi = None

    # A Sentinel-2 granule cannot cover a province-sized AOI.  Keep the
    # selected scene as the provenance anchor, but automatically render a
    # date-range mosaic when the AOI is larger than that granule footprint.
    if (
        not data.get("force_scene")
        and (data.get("force_mosaic") or data.get("auto_mosaic"))
        and provider_key in _SENTINEL2_MOSAIC_PROVIDER_KEYS
        and data.get("aoi")
        and data.get("start_date")
        and data.get("end_date")
    ):
        aoi = create_geometry_from_payload(data["aoi"])
        if data.get("force_mosaic") or _aoi_exceeds_scene_footprint(aoi, img):
            mosaic = _get_sentinel2_mosaic_tile(data, aoi, meta)
            if mosaic:
                return {
                    "scene_id": scene_id,
                    "tile_url": mosaic["tile_url"],
                    "satellite": meta,
                    "super_resolution": mosaic["super_resolution"],
                    "resolution_m": meta.get("resolution_m"),
                    "native_scale_m": meta.get("resolution_m"),
                    "dataset": meta.get("gee_collection"),
                    "visualization_bands": [
                        meta["band_role_map"]["red"],
                        meta["band_role_map"]["green"],
                        meta["band_role_map"]["blue"],
                    ],
                    "scene_count": mosaic["scene_count"],
                    "render_mode": "mosaic",
                    "aoi_larger_than_scene": True,
                    "cloud_mask_technique": mosaic["cloud_mask_technique"],
                }

    visualization = meta["visualization"]
    applied_cloud_mask = None

    if visualization == "rgb":
        # Optional per-pixel cloud mask (Sentinel-2 only, both L2A/L1C
        # variants) - user request: offer the same SCL/QA60/s2cloudless
        # techniques already used for Vegetation/Carbon, as an opt-in on top
        # of the default raw/unmasked view. Must run BEFORE .select(bands)
        # below, which drops every band not in band_role_map (SCL/QA60 included).
        available_techniques = meta.get("cloud_mask_techniques")
        requested_technique = data.get("cloud_mask_technique")
        if available_techniques and requested_technique:
            technique = resolve_cloud_mask_technique(requested_technique)
            if technique not in available_techniques:
                technique = available_techniques[0]
            img = _apply_s2_single_scene_mask(img, technique)
            applied_cloud_mask = technique

        band_role_map = meta["band_role_map"]
        if "reflectance_scale" in meta:
            bands = list(band_role_map.values())
            img = img.select(bands).multiply(meta["reflectance_scale"]).add(meta.get("reflectance_offset", 0))
        vis = {
            "bands": [band_role_map["red"], band_role_map["green"], band_role_map["blue"]],
            "min": meta["vis_min"],
            "max": meta["vis_max"],
        }
    elif visualization == "sar":
        sar_mode = data.get("sar_mode", "grayscale")  # "grayscale" | "composite"
        vv_band = meta["sar_bands"]["vv"]
        vh_band = meta["sar_bands"]["vh"]
        if sar_mode == "composite":
            ratio = img.select(vv_band).subtract(img.select(vh_band)).rename("ratio")
            img = ee.Image.cat([img.select(vv_band), img.select(vh_band), ratio])
            cv = meta["sar_composite_vis"]
            vis = {"bands": [vv_band, vh_band, "ratio"], "min": cv["min"], "max": cv["max"]}
        else:
            gv = meta["sar_grayscale_vis"]
            vis = {"bands": [gv["band"]], "min": gv["min"], "max": gv["max"]}
    elif visualization == "single_band":
        vis = {"bands": [meta["band"]], "min": meta["vis_min"], "max": meta["vis_max"], "palette": meta["palette"]}
    else:  # pragma: no cover - guarded by the registry itself, defensive only
        raise AnalysisError(f"Unknown visualization strategy '{visualization}' for {meta['name']}", 500)

    if visualization == "rgb":
        img = img.select(vis["bands"])
    img, super_resolution = _apply_super_resolution(img, meta, data.get("super_resolution"))

    if data.get("aoi"):
        if aoi is None:
            aoi = create_geometry_from_payload(data["aoi"])
        img = img.clip(aoi)

    tile = get_tile_url(img, vis, f"{meta['name']} scene")
    if not tile:
        raise AnalysisError("Gagal membuat tile untuk scene ini", 500)

    return {
        "scene_id": scene_id,
        "tile_url": tile["tile_url"],
        "satellite": meta,
        "super_resolution": super_resolution,
        "resolution_m": meta.get("resolution_m"),
        "native_scale_m": meta.get("resolution_m"),
        "dataset": meta.get("gee_collection"),
        "visualization_bands": vis.get("bands"),
        "scene_count": 1,
        "render_mode": "scene",
        "aoi_larger_than_scene": False,
        "cloud_mask_technique": applied_cloud_mask,
    }


@lru_cache(maxsize=512)
def _stac_asset_href(item_url: str, asset_key: str, provider_key: str | None = None) -> str:
    fetch_url = _resolve_commercial_item(provider_key, item_url) if provider_key in COMMERCIAL_PROVIDER_KEYS else item_url
    headers = commercial_request_headers(provider_key) if provider_key in COMMERCIAL_PROVIDER_KEYS else None
    timeout = max(5, get_settings().commercial_imagery_timeout_seconds) if provider_key in COMMERCIAL_PROVIDER_KEYS else 30
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers or {}) as client:
        item = _fetch_json(client, fetch_url)
    assets = item.get("assets") or {}
    asset = assets.get(asset_key)
    if not asset or not asset.get("href") or not _is_cog_like_asset(asset_key, asset):
        raise AnalysisError("Asset COG/GeoTIFF resolusi penuh yang dipilih tidak tersedia; pilih aset lain secara eksplisit", 404)
    return _abs_stac_href(fetch_url, asset["href"])


def _readable_stac_asset_href(item_url: str, asset_key: str, provider_key: str | None = None) -> str:
    href = _stac_asset_href(item_url, asset_key, provider_key)
    _validate_public_http_url(href, "URL asset COG")
    # Cache the stable asset URL, never an expiring SAS token.
    if urlparse(item_url).hostname == "planetarycomputer.microsoft.com":
        import planetary_computer
        href = planetary_computer.sign(href)
    return href


def _cog_tile_query(data: dict, item_url: str, default_asset_key: str = "visual") -> str:
    asset_key = str(data.get("cog_asset_key") or default_asset_key).strip() or default_asset_key
    parts = [f"item_url={quote(item_url, safe='')}", f"asset_key={quote(asset_key, safe='')}"]
    if data.get("cog_bands"):
        parts.append(f"bands={quote(str(data['cog_bands']), safe='')}")
    if data.get("cog_rescale"):
        parts.append(f"rescale={quote(str(data['cog_rescale']), safe='')}")
    return "&".join(parts)


def _parse_cog_bands(value: str | None) -> list[int] | None:
    if not value:
        return None
    try:
        bands = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise AnalysisError("Format band COG harus berupa angka dipisah koma, contoh: 1,2,3", 400) from exc
    if len(bands) not in {1, 3} or any(band < 1 for band in bands):
        raise AnalysisError("Pilih satu band atau tiga band RGB, dengan indeks mulai dari 1", 400)
    return bands


def _parse_cog_rescale(value: str | None, band_count: int) -> list[tuple[float, float]] | None:
    if not value:
        return None
    ranges: list[tuple[float, float]] = []
    try:
        for part in value.split("|"):
            if not part.strip():
                continue
            low, high = [float(v.strip()) for v in part.split(",", 1)]
            if not np.isfinite([low, high]).all() or high <= low:
                raise ValueError("invalid range")
            ranges.append((low, high))
    except ValueError as exc:
        raise AnalysisError("Format rescale COG harus seperti 0,3000 atau 0,3000|0,3000|0,3000", 400) from exc
    if not ranges:
        return None
    if len(ranges) == 1 and band_count > 1:
        ranges = ranges * band_count
    if len(ranges) != band_count:
        raise AnalysisError("Jumlah rentang rescale harus satu atau sama dengan jumlah band", 400)
    return ranges


def _render_cog_tile(item_url: str, z: int, x: int, y: int, asset_key: str, bands: str | None, rescale: str | None, provider_key: str | None = None) -> bytes | None:
    href = _readable_stac_asset_href(item_url, asset_key, provider_key)
    _validate_public_http_url(href, "URL asset COG")
    indexes = _parse_cog_bands(bands)
    with rasterio.Env():
        with Reader(href) as reader:
            if not reader.tile_exists(x, y, z):
                return None
            img = reader.tile(x, y, z, tilesize=256, indexes=indexes)
    data = img.data
    if data.shape[0] > 3:
        data = data[:3]
    if data.shape[0] == 1:
        data = np.repeat(data, 3, axis=0)
    rescale_ranges = _parse_cog_rescale(rescale, data.shape[0])
    rendered = ImageData(np.ma.array(data, mask=np.broadcast_to(img.mask == 0, data.shape)))
    if rescale_ranges:
        rendered.rescale(in_range=rescale_ranges)
    return rendered.render(img_format="PNG")


def get_stac_asset_download_url(item_url: str, asset_key: str, provider_key: str | None = None) -> str:
    href = _readable_stac_asset_href(item_url, asset_key, provider_key)
    _validate_public_http_url(href, "URL asset COG")
    return href


def render_stac_cog_tile(
    item_url: str,
    z: int,
    x: int,
    y: int,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
) -> bytes | None:
    try:
        return _render_cog_tile(item_url, z, x, y, asset_key, bands, rescale)
    except (TileOutsideBounds, PointOutsideBounds):
        return None
    except AnalysisError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("STAC COG tile render failed")
        raise AnalysisError(f"Gagal merender tile STAC COG: {exc}", 502)


def raster_toolbox(data: dict) -> dict:
    """Run small, bounded raster operations against the selected COG."""
    item_url = str(data.get("item_url") or "")
    asset_key = str(data.get("asset_key") or "visual")
    operation = str(data.get("operation") or "stretch").lower()
    href = _readable_stac_asset_href(item_url, asset_key)
    geometry = (data.get("aoi") or {}).get("geometry") or data.get("aoi")
    if not geometry:
        raise AnalysisError("AOI diperlukan untuk Raster Toolbox", 400)
    bands = _parse_cog_bands(data.get("bands")) or [1, 2, 3]
    with rasterio.Env(GDAL_HTTP_TIMEOUT=60, GDAL_HTTP_MAX_RETRY=2):
        with rasterio.open(href) as source:
            selected = [band for band in bands if band <= source.count]
            if not selected:
                raise AnalysisError("Band tidak tersedia pada asset COG", 400)
            source_geometry = warp_transform_geom("EPSG:4326", source.crs, geometry)
            clipped, transform = raster_mask(source, [source_geometry], crop=True, indexes=selected, filled=False)
            profile = source.profile.copy()
            profile.update(height=clipped.shape[1], width=clipped.shape[2], transform=transform, count=clipped.shape[0])
    values = clipped.compressed().astype("float32")
    if values.size == 0:
        raise AnalysisError("AOI tidak memotong area raster", 400)
    if operation in {"ndvi", "ndwi", "band_math"}:
        if clipped.shape[0] < 2:
            raise AnalysisError("Operasi indeks membutuhkan minimal dua band", 400)
        left = clipped[0].astype("float32")
        right = clipped[1].astype("float32")
        denominator = left + right
        result = np.ma.masked_where(denominator == 0, (left - right) / denominator)
        stats = {"min": float(result.min()), "mean": float(result.mean()), "max": float(result.max())}
        output = result.filled(np.nan).astype("float32")
    else:
        low, high = np.percentile(values, [2, 98])
        output = clipped[0].astype("float32")
        stats = {"min": float(low), "mean": float(np.mean(values)), "max": float(high)}
    histogram, edges = np.histogram(values, bins=32)
    result = {"operation": operation, "bands": selected, "stats": stats,
              "histogram": histogram.astype(int).tolist(), "bins": edges.astype(float).tolist(),
              "width": int(output.shape[-1]), "height": int(output.shape[-2])}
    if data.get("export"):
        from pathlib import Path
        export_dir = Path(get_settings().upload_dir) / "imagery-toolbox"
        export_dir.mkdir(parents=True, exist_ok=True)
        output_path = export_dir / f"toolbox-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.tif"
        out_profile = profile.copy()
        out_profile.update(dtype="float32", count=1, nodata=np.nan)
        with rasterio.open(output_path, "w", **out_profile) as target:
            target.write(output, 1)
        result["download_url"] = f"/api/imagery/toolbox-export/{quote(output_path.name)}"
    return result


def nasa_gibs_layers() -> list[dict[str, str]]:
    return [
        {"id": "MODIS_Terra_CorrectedReflectance_TrueColor", "name": "NASA MODIS True Color", "date_mode": "daily"},
        {"id": "VIIRS_SNPP_CorrectedReflectance_TrueColor", "name": "NASA VIIRS True Color", "date_mode": "daily"},
        {"id": "MODIS_Terra_Land_Surface_Temp_Day", "name": "NASA MODIS Land Surface Temperature", "date_mode": "daily"},
    ]


def render_maxar_open_data_tile(
    item_url: str,
    z: int,
    x: int,
    y: int,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
) -> bytes | None:
    try:
        return _render_cog_tile(item_url, z, x, y, asset_key, bands, rescale)
    except (TileOutsideBounds, PointOutsideBounds):
        return None
    except AnalysisError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Vantor/Maxar Open Data tile render failed")
        raise AnalysisError(f"Gagal merender tile Vantor/Maxar Open Data: {exc}", 502)


def render_commercial_stac_tile(
    provider_key: str,
    item_url: str,
    z: int,
    x: int,
    y: int,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
) -> bytes | None:
    if provider_key not in COMMERCIAL_PROVIDER_KEYS:
        raise AnalysisError("Provider commercial tidak dikenal", 400)
    if not commercial_configuration(provider_key)["configured"]:
        raise AnalysisError(f"{provider_key} belum dikonfigurasi di backend", 503)
    try:
        # The item lookup is authenticated. Providers should return a signed
        # COG href for range-based rendering; the credential is never sent to
        # the browser or embedded in the tile URL.
        return _render_cog_tile(item_url, z, x, y, asset_key, bands, rescale, provider_key)
    except (TileOutsideBounds, PointOutsideBounds):
        return None
    except AnalysisError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Commercial STAC tile render failed provider=%s", provider_key)
        raise AnalysisError(f"Gagal merender tile {provider_key}: {exc}", 502)


def get_dem_tile(data: dict) -> dict:
    """DEM/terrain layer for the imagery browser.

    DEMNAS can come from a configured Earth Engine asset, XYZ tile URL, or WMS.
    If none is configured, return an explicit SRTM fallback so the UI can still
    render terrain while clearly warning that it is not official DEMNAS.
    """
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)

    settings = get_settings()
    aoi = create_geometry_from_payload(data["aoi"])
    scale = int(data.get("scale", 30))
    elevation_vis = {"min": 0, "max": 3000, "palette": ["0b3d2e", "3f8f46", "f6d365", "c08457", "f7f7f7"]}

    if settings.demnas_ee_asset:
        dem = ee.Image(settings.demnas_ee_asset).rename("elevation").clip(aoi)
        tile = get_tile_url(dem, elevation_vis, "BIG DEMNAS elevation")
        stats = dem.reduceRegion(
            reducer=ee.Reducer.minMax().combine(ee.Reducer.mean(), sharedInputs=True),
            geometry=aoi,
            scale=scale,
            maxPixels=int(settings.max_pixels),
            bestEffort=True,
        ).getInfo()
        return {
            "tile_url": tile["tile_url"] if tile else None,
            "source": "BIG DEMNAS (configured Earth Engine asset)",
            "source_kind": "gee_asset",
            "is_official_demnas": True,
            "stats": {
                "min_elevation_m": stats.get("elevation_min"),
                "mean_elevation_m": stats.get("elevation_mean"),
                "max_elevation_m": stats.get("elevation_max"),
            },
            "legend": [
                {"label": "Rendah", "color": "#0b3d2e"},
                {"label": "Menengah", "color": "#f6d365"},
                {"label": "Tinggi", "color": "#f7f7f7"},
            ],
        }

    if settings.demnas_tile_url:
        return {
            "tile_url": settings.demnas_tile_url,
            "source": "BIG DEMNAS (configured XYZ tile service)",
            "source_kind": "xyz",
            "is_official_demnas": True,
            "stats": None,
            "legend": [],
        }

    if settings.demnas_wms_url and settings.demnas_wms_layers:
        return {
            "tile_url": None,
            "wms_url": settings.demnas_wms_url,
            "wms_layers": settings.demnas_wms_layers,
            "source": "BIG DEMNAS (configured WMS service)",
            "source_kind": "wms",
            "is_official_demnas": True,
            "stats": None,
            "legend": [],
        }

    dem = ee.Image("USGS/SRTMGL1_003").select("elevation").clip(aoi)
    tile = get_tile_url(dem, elevation_vis, "SRTM fallback elevation")
    stats = dem.reduceRegion(
        reducer=ee.Reducer.minMax().combine(ee.Reducer.mean(), sharedInputs=True),
        geometry=aoi,
        scale=90,
        maxPixels=int(settings.max_pixels),
        bestEffort=True,
    ).getInfo()
    return {
        "tile_url": tile["tile_url"] if tile else None,
        "source": "USGS SRTM fallback (DEMNAS belum dikonfigurasi)",
        "source_kind": "gee_asset",
        "is_official_demnas": False,
        "stats": {
            "min_elevation_m": stats.get("elevation_min"),
            "mean_elevation_m": stats.get("elevation_mean"),
            "max_elevation_m": stats.get("elevation_max"),
        },
        "legend": [
            {"label": "Rendah", "color": "#0b3d2e"},
            {"label": "Menengah", "color": "#f6d365"},
            {"label": "Tinggi", "color": "#f7f7f7"},
        ],
    }
