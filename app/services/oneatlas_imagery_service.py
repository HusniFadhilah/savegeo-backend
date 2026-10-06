"""Official Airbus OneAtlas Data OpenSearch and authenticated WMTS adapter.

This is intentionally provider-specific: OneAtlas Data Search is OpenSearch,
not STAC. Only search and WMTS preview are implemented. The adapter never
places orders or tasking requests and never returns provider access tokens or
provider asset URLs to the browser.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx

from app.core.config import Settings, get_settings
from app.services.gee_common import AnalysisError

logger = logging.getLogger(__name__)

_AUTH_URL = "https://authenticate.foundation.api.oneatlas.airbus.com/auth/realms/IDP/protocol/openid-connect/token"
_SEARCH_URL = "https://search.foundation.api.oneatlas.airbus.com/api/v2/opensearch"
_ACCESS_HOST = "access.foundation.api.oneatlas.airbus.com"
_MAX_PAGE_SIZE = 500
_MAX_PAGES = 4  # 2,000 scenes per request, matching the shared scene browser ceiling.
_MAX_TILE_BYTES = 5 * 1024 * 1024
_CACHE_TTL_SECONDS = 3600
_SCENE_ID_RE = re.compile(r"^oneatlas:[0-9a-f]{32}$")
_TOKEN_LOCK = threading.RLock()
_TOKEN: tuple[str, str, float] | None = None  # credential fingerprint, token, epoch expiry
_SCENES: OrderedDict[str, tuple[float, str, str, float | None]] = OrderedDict()
_SCENE_LOCK = threading.RLock()


@dataclass(frozen=True)
class OneAtlasConfiguration:
    api_key: str
    workspace_id: str


def _configuration(settings: Settings | None = None) -> OneAtlasConfiguration:
    settings = settings or get_settings()
    return OneAtlasConfiguration(
        api_key=str(settings.oneatlas_api_key or "").strip(),
        workspace_id=str(settings.oneatlas_workspace_id or "").strip(),
    )


def _get_token(client: httpx.Client, settings: Settings | None = None) -> str:
    global _TOKEN
    config = _configuration(settings)
    if not config.api_key:
        raise AnalysisError("Airbus OneAtlas belum dikonfigurasi. Isi ONEATLAS_API_KEY di backend.", 503)
    fingerprint = hashlib.sha256(config.api_key.encode("utf-8")).hexdigest()
    now = time.time()
    with _TOKEN_LOCK:
        if _TOKEN and _TOKEN[0] == fingerprint and _TOKEN[2] > now + 30:
            return _TOKEN[1]

    try:
        response = client.post(
            _AUTH_URL,
            data={"apikey": config.api_key, "grant_type": "api_key", "client_id": "IDP"},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
            follow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            raise AnalysisError("OneAtlas authentication redirect was blocked", 502)
        if response.status_code in {401, 403}:
            raise AnalysisError("OneAtlas authentication failed (401/403). Check ONEATLAS_API_KEY and account access.", 503)
        if response.status_code == 429:
            raise AnalysisError("OneAtlas authentication is rate limited (429). Retry later.", 429)
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AnalysisError("OneAtlas authentication returned an invalid token response", 502)
        expires_in = payload.get("expires_in", 3000)
        try:
            lifetime = max(60, min(int(expires_in) - 60, 3500))
        except (TypeError, ValueError):
            lifetime = 3000
    except AnalysisError:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise AnalysisError(f"OneAtlas authentication failed (HTTP {status}); verify account API access.", 503)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OneAtlas authentication request failed: %s", type(exc).__name__)
        raise AnalysisError("Could not authenticate with OneAtlas. Check network access and retry.", 502)

    with _TOKEN_LOCK:
        _TOKEN = (fingerprint, token, now + lifetime)
    return token


def _geojson_geometry(aoi: Any) -> dict[str, Any]:
    if not isinstance(aoi, dict):
        raise AnalysisError("AOI harus berupa GeoJSON atau extent peta", 400)
    geometry = aoi.get("geojson") or aoi.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") in {
        "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection",
    } and geometry.get("coordinates") is not None:
        return geometry
    bounds = aoi.get("bbox") or aoi
    if isinstance(bounds, dict) and all(key in bounds for key in ("west", "south", "east", "north")):
        west, south, east, north = (float(bounds[key]) for key in ("west", "south", "east", "north"))
        if not all(math.isfinite(value) for value in (west, south, east, north)):
            raise AnalysisError("Extent AOI tidak valid", 400)
        if west >= east or south >= north or west < -180 or east > 180 or south < -90 or north > 90:
            raise AnalysisError("Extent AOI berada di luar koordinat WGS84", 400)
        return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}
    raise AnalysisError("AOI tidak berisi geometri GeoJSON atau extent peta yang valid", 400)


def _date_boundary(value: Any, *, end: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisError("start_date dan end_date harus diisi", 400)
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalysisError("Rentang tanggal OneAtlas tidak valid", 400) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if len(raw) == 10 and end:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999000)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def _scene_ref(provider_scene_id: str) -> str:
    token = hashlib.sha256(provider_scene_id.encode("utf-8")).hexdigest()[:32]
    return f"oneatlas:{token}"


def _wmts_link(feature: dict[str, Any]) -> str | None:
    rights = feature.get("rights")
    links = feature.get("_links")
    wmts = links.get("wmts") if isinstance(links, dict) else None
    href = wmts.get("href") if isinstance(wmts, dict) else None
    if not isinstance(rights, dict) or not rights.get("wmts") or not isinstance(href, str):
        return None
    parsed = urlparse(href)
    if (
        parsed.scheme != "https" or parsed.hostname != _ACCESS_HOST or parsed.port is not None
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        return None
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) != 5 or path_parts[:3] != ["api", "v1", "items"] or path_parts[4] != "wmts":
        return None
    try:
        UUID(path_parts[3])
    except ValueError:
        return None
    return href.rstrip("/")


def _remember_scene(scene_ref: str, provider_scene_id: str, wmts_href: str | None, resolution_m: float | None) -> None:
    now = time.time()
    with _SCENE_LOCK:
        for key, (expires, *_rest) in list(_SCENES.items()):
            if expires <= now:
                _SCENES.pop(key, None)
        _SCENES[scene_ref] = (now + _CACHE_TTL_SECONDS, provider_scene_id, wmts_href or "", resolution_m)
        _SCENES.move_to_end(scene_ref)
        while len(_SCENES) > 10_000:
            _SCENES.popitem(last=False)


def _cached_scene(scene_ref: str) -> tuple[str, str, float | None]:
    if not _SCENE_ID_RE.fullmatch(scene_ref):
        raise AnalysisError("Referensi scene OneAtlas tidak valid", 400)
    with _SCENE_LOCK:
        cached = _SCENES.get(scene_ref)
        if not cached or cached[0] <= time.time():
            _SCENES.pop(scene_ref, None)
            raise AnalysisError("Referensi scene OneAtlas sudah kedaluwarsa; lakukan search ulang", 410)
        _SCENES.move_to_end(scene_ref)
        return cached[1], cached[2], cached[3]


def _normalize_feature(feature: Any, request=None) -> dict[str, Any] | None:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        return None
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    provider_id = props.get("id") or feature.get("id")
    acquired_at = _utc_iso(props.get("acquisitionDate"))
    if not isinstance(provider_id, str) or not provider_id or not acquired_at:
        return None
    public_id = _scene_ref(provider_id)
    resolution_m = _number(props.get("resolution"), minimum=0.001)
    cloud = _number(props.get("cloudCover"), minimum=0, maximum=100)
    wmts_href = _wmts_link(feature)
    _remember_scene(public_id, provider_id, wmts_href, resolution_m)
    assets = []
    if wmts_href:
        base = str(request.base_url).rstrip("/") if request is not None else ""
        internal_href = f"{base}/api/imagery/oneatlas-tiles/{quote(public_id, safe=':')}/{{z}}/{{x}}/{{y}}.png"
        assets = [{
            "key": "wmts_rgb",
            "title": "Authenticated provider WMTS RGB preview",
            "href": internal_href,
            "type": "image/png",
            "roles": ["visual"],
            "resolution_m": resolution_m,
        }]
    return {
        "id": public_id,
        "acquired_at": acquired_at,
        "cloud_cover_pct": round(cloud, 1) if cloud is not None else None,
        "bbox": feature.get("bbox"),
        "footprint": feature.get("geometry"),
        "resolution_m": resolution_m,
        "platform": props.get("platform") or props.get("constellation"),
        "producer": props.get("organisationName") or "Airbus Defence and Space",
        "title": props.get("title") or props.get("sourceIdentifier") or provider_id,
        "assets": assets,
        "default_asset_key": "wmts_rgb" if wmts_href else None,
    }


def list_scenes(data: dict[str, Any], request=None, *, client_factory: Callable[..., httpx.Client] = httpx.Client) -> dict[str, Any]:
    config = _configuration()
    if not config.api_key or not config.workspace_id:
        raise AnalysisError("Airbus OneAtlas belum siap. Isi ONEATLAS_API_KEY dan ONEATLAS_WORKSPACE_ID di backend; preview juga memerlukan hak WMTS/streaming pada akun.", 503)
    geometry = _geojson_geometry(data.get("aoi"))
    start = _date_boundary(data.get("start_date"))
    end = _date_boundary(data.get("end_date"), end=True)
    if start > end:
        raise AnalysisError("start_date harus sebelum atau sama dengan end_date", 400)
    max_cloud = _number(data.get("max_cloud_cover"), minimum=0, maximum=100)
    if data.get("max_cloud_cover") is not None and max_cloud is None:
        raise AnalysisError("max_cloud_cover harus di antara 0 dan 100", 400)

    timeout = max(5, int(get_settings().commercial_imagery_timeout_seconds or 30))
    scenes: list[dict[str, Any]] = []
    truncated = False
    page_count = 0
    try:
        with client_factory(timeout=timeout, follow_redirects=False) as client:
            token = _get_token(client)
            for page in range(1, _MAX_PAGES + 1):
                body: dict[str, Any] = {
                    "itemsPerPage": _MAX_PAGE_SIZE,
                    "startPage": page,
                    "acquisitionDate": f"[{start},{end}]",
                    "processingLevel": "SENSOR",
                    "relation": "intersects",
                    "geometry": geometry,
                }
                if max_cloud is not None:
                    body["cloudCover"] = f"[0,{max_cloud:g}]"
                response = client.post(
                    _SEARCH_URL,
                    params={"workspaceId": config.workspace_id},
                    json=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Cache-Control": "no-cache"},
                    follow_redirects=False,
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise AnalysisError("OneAtlas search redirect was blocked", 502)
                if response.status_code in {401, 403}:
                    raise AnalysisError("OneAtlas search denied (401/403). Check account workspace, API access, and product entitlement.", 503)
                if response.status_code == 429:
                    raise AnalysisError("OneAtlas search is rate limited (429). Retry later.", 429)
                response.raise_for_status()
                payload = response.json()
                features = payload.get("features") if isinstance(payload, dict) else None
                if not isinstance(features, list):
                    raise AnalysisError("OneAtlas returned a malformed OpenSearch response (expected features[])", 502)
                page_count = len(features)
                for feature in features:
                    normalized = _normalize_feature(feature, request)
                    if normalized:
                        scenes.append(normalized)
                        if len(scenes) >= _MAX_PAGE_SIZE * _MAX_PAGES:
                            truncated = True
                            break
                if truncated or page_count < _MAX_PAGE_SIZE:
                    break
                if page == _MAX_PAGES:
                    truncated = True
    except AnalysisError:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("OneAtlas OpenSearch failed status=%s", status)
        raise AnalysisError(f"OneAtlas search failed (HTTP {status}). Check account access or retry later.", 502)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OneAtlas OpenSearch failed: %s", type(exc).__name__)
        raise AnalysisError("OneAtlas search failed due to a network or response error. Retry or contact the provider administrator.", 502)

    scenes.sort(key=lambda scene: scene.get("acquired_at") or "", reverse=True)
    from app.services.imagery_service import imagery_provider_response_meta

    return {"scenes": scenes, "count": len(scenes), "satellite": imagery_provider_response_meta("airbus_oneatlas"), "truncated": truncated}


def get_scene_tile(data: dict[str, Any], request=None) -> dict[str, Any]:
    config = _configuration()
    if not config.api_key or not config.workspace_id:
        raise AnalysisError("Airbus OneAtlas belum dikonfigurasi. Isi ONEATLAS_API_KEY dan ONEATLAS_WORKSPACE_ID di backend.", 503)
    scene_ref = str(data.get("scene_id") or "")
    _provider_id, wmts_href, resolution_m = _cached_scene(scene_ref)
    if not wmts_href:
        raise AnalysisError("Scene OneAtlas ini tidak memberi hak WMTS preview. Tidak ada stream atau pembelian yang akan dimulai.", 403)
    if request is None:
        raise AnalysisError("Request context is required for OneAtlas preview tiles", 500)
    from app.services.imagery_service import imagery_provider_response_meta

    base = str(request.base_url).rstrip("/")
    return {
        "scene_id": scene_ref,
        "tile_url": f"{base}/api/imagery/oneatlas-tiles/{quote(scene_ref, safe=':')}/{{z}}/{{x}}/{{y}}.png",
        "satellite": imagery_provider_response_meta("airbus_oneatlas"),
        "resolution_m": resolution_m,
        "super_resolution": None,
        "render_mode": "scene",
    }


def render_tile(scene_ref: str, z: int, x: int, y: int, *, client_factory: Callable[..., httpx.Client] = httpx.Client) -> bytes | None:
    config = _configuration()
    if not config.api_key or not config.workspace_id:
        raise AnalysisError("Airbus OneAtlas belum dikonfigurasi. Isi ONEATLAS_API_KEY dan ONEATLAS_WORKSPACE_ID di backend.", 503)
    if not (0 <= z <= 22 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise AnalysisError("Tile coordinates are invalid", 400)
    _provider_id, wmts_href, _resolution_m = _cached_scene(scene_ref)
    if not wmts_href:
        raise AnalysisError("Scene OneAtlas ini tidak memberi hak WMTS preview", 403)
    # _wmts_link accepts only the documented Airbus access host and item WMTS
    # path. The browser supplies only an opaque scene id and XYZ coordinates.
    tile_url = f"{wmts_href}/tiles/1.0.0/default/rgb/EPSG3857/{z}/{x}/{y}.png"
    parsed = urlparse(tile_url)
    if parsed.scheme != "https" or parsed.hostname != _ACCESS_HOST or parsed.username or parsed.password:
        raise AnalysisError("OneAtlas WMTS URL failed validation", 400)
    timeout = max(5, int(get_settings().commercial_imagery_timeout_seconds or 30))
    try:
        with client_factory(timeout=timeout, follow_redirects=False) as client:
            token = _get_token(client)
            with client.stream("GET", tile_url, headers={"Authorization": f"Bearer {token}", "Cache-Control": "no-cache"}, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise AnalysisError("OneAtlas WMTS redirect was blocked", 502)
                if response.status_code in {401, 403}:
                    raise AnalysisError("OneAtlas preview denied (401/403). Check WMTS rights and streaming entitlement for this scene.", 403)
                if response.status_code == 429:
                    raise AnalysisError("OneAtlas preview is rate limited (429). Retry later.", 429)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"image/png", "application/octet-stream"}:
                    raise AnalysisError("OneAtlas WMTS returned a non-image response", 502)
                size = 0
                chunks = []
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_TILE_BYTES:
                        raise AnalysisError("OneAtlas WMTS tile exceeded the response size limit", 502)
                    chunks.append(chunk)
                return b"".join(chunks)
    except AnalysisError:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning("OneAtlas WMTS failed status=%s", exc.response.status_code)
        raise AnalysisError(f"OneAtlas preview failed (HTTP {exc.response.status_code})", 502)
    except httpx.HTTPError as exc:
        logger.warning("OneAtlas WMTS failed: %s", type(exc).__name__)
        raise AnalysisError("OneAtlas preview failed due to a network error. Retry or contact the provider administrator.", 502)
