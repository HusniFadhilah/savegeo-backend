"""Raw satellite imagery browser - /api/imagery/providers (static catalog),
/api/imagery/scenes (list real scenes with their exact acquisition date+time)
and /api/imagery/scene-tile (RGB/SAR/gas-colormap tile for one single scene,
no compositing) - independent of vegetation/landcover/carbon analysis. See
app.services.imagery_service for the full rationale.
"""
from __future__ import annotations

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import require_ee
from app.services import imagery_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["imagery"])
logger = logging.getLogger(__name__)


@router.get("/imagery/providers")
def imagery_providers():
    """Static (no GEE) catalog of selectable satellite/sensor providers for the
    scene browser - separate from GET /vegetation/satellites (index-analysis
    catalog); see imagery_provider_registry.py for why."""
    return imagery_service.list_providers()


@router.post("/imagery/scenes", dependencies=[Depends(require_ee)])
async def imagery_scenes(request: Request):
    data = await request.json()
    try:
        return imagery_service.list_scenes(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery scenes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/imagery/scene-tile", dependencies=[Depends(require_ee)])
async def imagery_scene_tile(request: Request):
    data = await request.json()
    try:
        return imagery_service.get_scene_tile(data)
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
