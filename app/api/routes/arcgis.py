"""ArcGIS ImageServer XYZ tile proxy - /api/arcgis/tiles/{dataset_key}/{z}/{y}/{x}.

Ported (business logic unchanged) from legacy `backend/app.py::arcgis_tile_proxy`
(lines ~1953-2093). Non-cached ArcGIS ImageServer services are proxied as XYZ
tiles via exportImage, with bbox derived from tile coordinates. Auth token is
added server-side and never exposed to the client.
"""
from __future__ import annotations

import json
import logging
import math

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.providers.arcgis_client import _is_allowed_domain, get_arcgis_client
from app.registries.carbon_dataset_registry import get_arcgis_carbon_meta, is_arcgis_carbon_dataset
from app.registries.landcover_dataset_registry import LAND_COVER_DATASET_OPTIONS, LAND_COVER_LEGENDS
from app.services.arcgis_helpers import _get_clip_poly, _year_ms_range

router = APIRouter(tags=["arcgis"])
logger = logging.getLogger(__name__)


@router.get("/arcgis/tiles/{dataset_key}/{z}/{y}/{x}")
def arcgis_tile_proxy(
    dataset_key: str,
    z: int,
    y: int,
    x: int,
    year: int | None = None,
    clip_west: float | None = None,
    clip_south: float | None = None,
    clip_east: float | None = None,
    clip_north: float | None = None,
    clip_key: str | None = None,
):
    """
    Proxy ArcGIS ImageServer as XYZ tiles via exportImage.
    Non-cached ImageServer services use exportImage with bbox derived from tile coords.
    Auth token added server-side and never exposed to client.
    """
    _is_carbon_tile = is_arcgis_carbon_dataset(dataset_key)

    if _is_carbon_tile:
        ds_meta = get_arcgis_carbon_meta(dataset_key) or {}
    else:
        ds_meta = LAND_COVER_DATASET_OPTIONS.get(dataset_key, {})

    if not ds_meta.get("provider_type", "").startswith("arcgis"):
        raise HTTPException(status_code=400, detail="Not an ArcGIS dataset")

    service_url = ds_meta.get("arcgis_service_url", "").rstrip("/")
    if not service_url or not _is_allowed_domain(service_url):
        raise HTTPException(status_code=403, detail="Service URL not allowed")

    client = get_arcgis_client()
    if not client.is_enabled():
        raise HTTPException(status_code=503, detail="ArcGIS integration not enabled")

    # Convert tile z/x/y -> geographic bbox (WGS84)
    n = 2 ** z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))

    # AOI clip bbox - encoded by compute_arcgis_landcover_summary
    clip_bbox = (
        (clip_west, clip_south, clip_east, clip_north)
        if all(v is not None for v in (clip_west, clip_south, clip_east, clip_north))
        else None
    )

    # Fast-reject: tile completely outside AOI bbox -> transparent 1x1
    if clip_bbox:
        cw, cs, ce, cn = clip_bbox
        if lon_max <= cw or lon_min >= ce or lat_max <= cs or lat_min >= cn:
            return Response(content=b"", status_code=200, media_type="image/png")

    params = {
        "bbox": f"{lon_min},{lat_min},{lon_max},{lat_max}",
        "bboxSR": "4326",
        "size": "256,256",
        "imageSR": "3857",  # Web Mercator for correct Leaflet alignment
        "format": "png32",
        "transparent": "true",
        "f": "image",
    }

    # Apply rendering rule based on dataset type
    if _is_carbon_tile:
        # Continuous carbon raster: stretch + color ramp
        palette = ds_meta.get("vis_palette", ["f7fcf5", "74c476", "00441b"])
        c_min = ds_meta.get("vis_min", 0)
        c_max = ds_meta.get("vis_max", 300)
        n_colors = len(palette)
        colormap_entries = []
        for i, hex_c in enumerate(palette):
            val = c_min + (c_max - c_min) * i / max(n_colors - 1, 1)
            h = hex_c.lstrip("#")
            colormap_entries.append([round(val, 1), int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)])
        params["renderingRule"] = json.dumps({
            "rasterFunction": "Colormap",
            "rasterFunctionArguments": {"colormap": colormap_entries},
        })
    elif ds_meta.get("class_schema"):
        # Categorical land cover: explicit class colormap
        legend = LAND_COVER_LEGENDS.get(dataset_key, {})
        colormap = []
        for key, meta in legend.items():
            try:
                val = int(key)
                hex_c = meta["color"].lstrip("#")
                colormap.append([val, int(hex_c[0:2], 16), int(hex_c[2:4], 16), int(hex_c[4:6], 16)])
            except (ValueError, KeyError):
                continue
        if colormap:
            params["renderingRule"] = json.dumps({
                "rasterFunction": "Colormap",
                "rasterFunctionArguments": {"colormap": colormap},
            })

    # Chain ArcGIS Clip raster function to mask pixels outside AOI polygon
    if clip_bbox:
        cw, cs, ce, cn = clip_bbox
        # Prefer full polygon from cache; fall back to bbox rectangle
        cached_poly = _get_clip_poly(clip_key) if clip_key else None
        clip_geom = cached_poly or {
            "rings": [[[cw, cs], [ce, cs], [ce, cn], [cw, cn], [cw, cs]]],
            "spatialReference": {"wkid": 4326},
        }
        clip_rule = {
            "rasterFunction": "Clip",
            "rasterFunctionArguments": {
                "ClippingType": 1,
                "ClippingGeometry": clip_geom,
            },
        }
        if "renderingRule" in params:
            clip_rule["rasterFunctionArguments"]["Raster"] = json.loads(params["renderingRule"])
        params["renderingRule"] = json.dumps(clip_rule)

    # Only send time filter for time-aware services
    if year and ds_meta.get("time_aware", True) and not _is_carbon_tile:
        params["time"] = _year_ms_range(year)

    if ds_meta.get("requires_auth", False):
        params.update(client._get_auth_params())

    try:
        # POST avoids URL length limits when renderingRule contains a complex polygon
        resp = requests.post(f"{service_url}/exportImage", data=params, timeout=20)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image"):
            return Response(
                content=resp.content,
                media_type=resp.headers.get("Content-Type", "image/png"),
                headers={"Cache-Control": "public, max-age=3600"},
            )
        # Forward non-image responses as transparent tile to avoid Leaflet errors
        return Response(content=b"", status_code=200, media_type="image/png")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"ArcGIS tile proxy error [{dataset_key} z={z} y={y} x={x}]: {exc}")
        return Response(content=b"", status_code=200, media_type="image/png")
