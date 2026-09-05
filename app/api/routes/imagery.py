"""Raw satellite imagery browser - /api/imagery/providers (static catalog),
/api/imagery/scenes (list real scenes with their exact acquisition date+time)
and /api/imagery/scene-tile (RGB/SAR/gas-colormap tile for one single scene,
no compositing) - independent of vegetation/landcover/carbon analysis. See
app.services.imagery_service for the full rationale.
"""
from __future__ import annotations

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import require_ee
from app.services import copernicus_geosave_service
from app.services import imagery_service
from app.services import samgeo_service
from app.services.copernicus_geosave_service import CopernicusAnalysisError
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["imagery"])
logger = logging.getLogger(__name__)


@router.get("/imagery/samgeo/status")
def imagery_samgeo_status():
    return samgeo_service.capabilities()


@router.post("/imagery/samgeo/jobs", status_code=202)
def imagery_samgeo_start(data: dict):
    try:
        return samgeo_service.start_job(data)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/imagery/samgeo/jobs/{job_id}")
def imagery_samgeo_result(job_id: str):
    try:
        return samgeo_service.get_job(job_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/imagery/providers")
def imagery_providers():
    """Static (no GEE) catalog of selectable satellite/sensor providers for the
    scene browser - separate from GET /vegetation/satellites (index-analysis
    catalog); see imagery_provider_registry.py for why."""
    return imagery_service.list_providers()


def _is_copernicus_request(data: dict) -> bool:
    return copernicus_geosave_service.is_copernicus_provider(data.get("satellite"))


def _requires_ee(data: dict) -> bool:
    return data.get("satellite") not in {
        "openaerialmap",
        "vantor_open_data",
        "planet_open_data",
        "stac_catalog",
    } and not _is_copernicus_request(data)


@router.post("/imagery/scenes")
async def imagery_scenes(request: Request):
    data = await request.json()
    try:
        if _requires_ee(data):
            require_ee(request)
        return imagery_service.list_scenes(data, request)
    except CopernicusAnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery scenes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/imagery/scene-tile")
async def imagery_scene_tile(request: Request):
    data = await request.json()
    try:
        if _requires_ee(data):
            require_ee(request)
        return imagery_service.get_scene_tile(data, request)
    except CopernicusAnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery scene-tile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/imagery/dem-tile", dependencies=[Depends(require_ee)])
async def imagery_dem_tile(request: Request):
    data = await request.json()
    try:
        return imagery_service.get_dem_tile(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery DEM tile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/imagery/copernicus-tiles/{provider_key}/{scene_id}/{z}/{x}/{y}.png")
def imagery_copernicus_tile(provider_key: str, scene_id: str, z: int, x: int, y: int):
    try:
        png_bytes = copernicus_geosave_service.render_tile(provider_key, scene_id, z, x, y)
    except CopernicusAnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery Copernicus tile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/imagery/maxar-open-data-tiles/{z}/{x}/{y}.png")
def imagery_maxar_open_data_tile(
    z: int,
    x: int,
    y: int,
    item_url: str,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
):
    try:
        png_bytes = imagery_service.render_maxar_open_data_tile(item_url, z, x, y, asset_key, bands, rescale)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery Vantor/Maxar Open Data tile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/imagery/stac-cog-tiles/{z}/{x}/{y}.png")
def imagery_stac_cog_tile(
    z: int,
    x: int,
    y: int,
    item_url: str,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
):
    try:
        png_bytes = imagery_service.render_stac_cog_tile(item_url, z, x, y, asset_key, bands, rescale)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery STAC COG tile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/imagery/stac-source")
def imagery_stac_source(item_url: str, asset_key: str = "visual"):
    try:
        href = imagery_service.get_stac_asset_download_url(item_url, asset_key)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return RedirectResponse(href)
