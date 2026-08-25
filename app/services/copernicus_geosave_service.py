"""Copernicus CDSE ingestion bridge backed by geosave-engine's STAC client.

This keeps the raw scene browser's existing contract intact: list exact
acquisition scenes, then return a Leaflet XYZ URL for one scene. GeoSave Engine
does the STAC-side discovery; this module adapts its items into SAVEGEO's UI.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import numpy as np
import rasterio
from dotenv import dotenv_values
from fastapi import Request
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.models import ImageData

logger = logging.getLogger(__name__)

_MAX_SCENES = 100
_CDSE_TIMEOUT_SECONDS = 20
_S2_L2A_COLLECTION = "sentinel-2-l2a"
_S2_L1C_COLLECTION = "sentinel-2-l1c"
_RGB_ASSETS = (("B04_10m", "B04"), ("B03_10m", "B03"), ("B02_10m", "B02"))
_S2_VIS_RANGE = ((0, 3000), (0, 3000), (0, 3000))


class CopernicusAnalysisError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


COPERNICUS_PROVIDERS: dict[str, dict[str, Any]] = {
    "copernicus_s2_l2a": {
        "key": "copernicus_s2_l2a",
        "name": "Copernicus CDSE Sentinel-2 L2A (GeoSave)",
        "provider": "ESA / Copernicus Data Space Ecosystem",
        "group": "Sentinel-2",
        "gee_collection": "",
        "stac_collection": _S2_L2A_COLLECTION,
        "source_kind": "geosave_cdse_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 10,
        "revisit_days": 5,
        "start_year": 2017,
        "cloud_property": "eo:cloud_cover",
        "cloud_mask_techniques": None,
        "description": (
            "Scene Sentinel-2 dari Copernicus CDSE via geosave-engine/STAC. "
            "Digunakan untuk ingestion lokal dan browsing tanpa Google Earth Engine."
        ),
    },
    "copernicus_s2_l1c": {
        "key": "copernicus_s2_l1c",
        "name": "Copernicus CDSE Sentinel-2 L1C (GeoSave)",
        "provider": "ESA / Copernicus Data Space Ecosystem",
        "group": "Sentinel-2",
        "gee_collection": "",
        "stac_collection": _S2_L1C_COLLECTION,
        "source_kind": "geosave_cdse_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 10,
        "revisit_days": 5,
        "start_year": 2015,
        "cloud_property": "eo:cloud_cover",
        "cloud_mask_techniques": None,
        "description": (
            "Top-of-atmosphere Sentinel-2 dari Copernicus CDSE via geosave-engine/STAC "
            "untuk arsip 2015+."
        ),
    },
}


def is_copernicus_provider(provider_key: str | None) -> bool:
    return provider_key in COPERNICUS_PROVIDERS


def get_provider(provider_key: str | None) -> dict[str, Any]:
    if provider_key not in COPERNICUS_PROVIDERS:
        raise CopernicusAnalysisError("Provider Copernicus tidak dikenal", 400)
    return COPERNICUS_PROVIDERS[provider_key]


@lru_cache(maxsize=1)
def _client():
    # Import lazily so app startup still works in environments where the
    # optional ingestion stack has not been installed yet.
    from geosave_engine.geodata.stac import StacClient
    from pystac_client.stac_api_io import StacApiIO
    from urllib3.util import Retry

    client = StacClient.cdse()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    client._client._stac_io = StacApiIO(max_retries=retry_strategy, timeout=_CDSE_TIMEOUT_SECONDS)
    return client


def _datetime_range(start_date: str, end_date: str) -> str:
    return f"{start_date}T00:00:00Z/{end_date}T23:59:59Z"


def _geojson_from_aoi(aoi_payload: dict) -> dict:
    if not isinstance(aoi_payload, dict):
        raise CopernicusAnalysisError("AOI payload must be an object", 400)
    if "geojson" in aoi_payload:
        geojson = aoi_payload["geojson"]
        if geojson.get("type") == "FeatureCollection":
            return geojson["features"][0]["geometry"]
        if geojson.get("type") == "Feature":
            return geojson["geometry"]
        return geojson
    required = {"west", "south", "east", "north"}
    if not required.issubset(aoi_payload.keys()):
        raise CopernicusAnalysisError("AOI bounds missing west/south/east/north", 400)
    west, south, east, north = (float(aoi_payload[k]) for k in ("west", "south", "east", "north"))
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def _item_cloud(item: Any) -> float | None:
    value = item.properties.get("eo:cloud_cover")
    return round(float(value), 1) if value is not None else None


def _item_acquired(item: Any) -> str:
    raw = item.properties.get("datetime")
    if isinstance(raw, str):
        return raw.replace("+00:00", "Z")
    if isinstance(raw, datetime):
        return raw.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return ""


def _tile_url(request: Request, provider_key: str, scene_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/imagery/copernicus-tiles/{provider_key}/{scene_id}/{{z}}/{{x}}/{{y}}.png"


def _asset_href_and_env(asset: Any) -> tuple[str, dict[str, str]]:
    env_file = dotenv_values(".env")
    token = (
        os.environ.get("COPERNICUS_CDSE_ACCESS_TOKEN")
        or env_file.get("COPERNICUS_CDSE_ACCESS_TOKEN")
        or ""
    ).strip()
    https_alt = asset.extra_fields.get("alternate", {}).get("https", {})
    if token and https_alt.get("href"):
        return https_alt["href"], {"GDAL_HTTP_HEADERS": f"Authorization: Bearer {token}"}

    if asset.href.startswith("s3://") and not (
        os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
    ):
        raise CopernicusAnalysisError(
            "Pixel asset Copernicus CDSE membutuhkan kredensial. Isi "
            "COPERNICUS_CDSE_ACCESS_TOKEN untuk OIDC HTTPS, atau set AWS_ACCESS_KEY_ID/"
            "AWS_SECRET_ACCESS_KEY untuk akses S3 CDSE.",
            503,
        )

    return asset.href, {}


def list_scenes(data: dict) -> dict:
    if not data.get("aoi"):
        raise CopernicusAnalysisError("aoi is required", 400)
    if not data.get("start_date") or not data.get("end_date"):
        raise CopernicusAnalysisError("start_date and end_date are required", 400)

    provider_key = data.get("satellite")
    provider = get_provider(provider_key)
    aoi = _geojson_from_aoi(data["aoi"])
    max_cloud_cover = data.get("max_cloud_cover")

    from geosave_engine.geodata.stac import StacQuery

    query = StacQuery(
        collections=[provider["stac_collection"]],
        intersects=aoi,
        datetime=_datetime_range(data["start_date"], data["end_date"]),
        max_items=_MAX_SCENES,
        limit=min(int(data.get("limit") or _MAX_SCENES), _MAX_SCENES),
        sortby=[{"field": "datetime", "direction": "asc"}],
    )
    if max_cloud_cover is not None:
        query = query.with_filter(f"eo:cloud_cover <= {float(max_cloud_cover)}")

    old_timeout = os.environ.get("GDAL_HTTP_TIMEOUT")
    os.environ["GDAL_HTTP_TIMEOUT"] = str(_CDSE_TIMEOUT_SECONDS)
    try:
        items = _client().search(query)
    except Exception as e:  # noqa: BLE001
        logger.exception("CDSE STAC search failed")
        raise CopernicusAnalysisError(f"Gagal mengakses Copernicus CDSE STAC: {e}", 502)
    finally:
        if old_timeout is None:
            os.environ.pop("GDAL_HTTP_TIMEOUT", None)
        else:
            os.environ["GDAL_HTTP_TIMEOUT"] = old_timeout

    scenes = [
        {"id": item.id, "acquired_at": _item_acquired(item), "cloud_cover_pct": _item_cloud(item)}
        for item in items
    ]
    return {
        "scenes": scenes,
        "count": len(scenes),
        "satellite": provider,
        "truncated": len(scenes) >= _MAX_SCENES,
    }


def get_scene_tile(data: dict, request: Request) -> dict:
    if not data.get("scene_id"):
        raise CopernicusAnalysisError("scene_id is required", 400)
    provider_key = data.get("satellite")
    provider = get_provider(provider_key)
    scene_id = data["scene_id"]

    return {
        "scene_id": scene_id,
        "tile_url": _tile_url(request, provider["key"], scene_id),
        "satellite": provider,
    }


@lru_cache(maxsize=256)
def _get_item(provider_key: str, scene_id: str):
    provider = get_provider(provider_key)
    from geosave_engine.geodata.stac import StacQuery

    items = _client().search(
        StacQuery(collections=[provider["stac_collection"]], ids=[scene_id], max_items=1)
    )
    if not items:
        raise CopernicusAnalysisError(f"Scene '{scene_id}' tidak ditemukan di Copernicus CDSE", 404)
    return items[0]


def render_tile(provider_key: str, scene_id: str, z: int, x: int, y: int) -> bytes | None:
    item = _get_item(provider_key, scene_id)
    arrays: list[np.ndarray] = []
    mask = None

    try:
        for asset_names in _RGB_ASSETS:
            asset_name = next((name for name in asset_names if name in item.assets), asset_names[0])
            asset = item.assets.get(asset_name)
            if asset is None:
                raise CopernicusAnalysisError(
                    f"Asset {asset_name} tidak tersedia untuk scene {scene_id}",
                    404,
                )
            href, env = _asset_href_and_env(asset)
            with rasterio.Env(**env):
                with Reader(href) as reader:
                    if not reader.tile_exists(x, y, z):
                        return None
                    img = reader.tile(x, y, z, tilesize=256)
            arrays.append(img.data[0])
            mask = img.mask if mask is None else np.minimum(mask, img.mask)
    except (TileOutsideBounds, PointOutsideBounds):
        return None
    except CopernicusAnalysisError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("CDSE tile render failed")
        raise CopernicusAnalysisError(f"Gagal merender tile Copernicus CDSE: {e}", 502)

    data = np.stack(arrays)
    rendered = ImageData(data, mask=mask)
    rendered.rescale(in_range=_S2_VIS_RANGE)
    return rendered.render(img_format="PNG")
