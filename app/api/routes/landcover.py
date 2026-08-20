"""Land cover analysis endpoints - /api/analyze/landcover*. Ported from backend/app.py."""
from __future__ import annotations

import ee
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import require_ee
from app.services import landcover_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["landcover"])
logger = logging.getLogger(__name__)


@router.post("/analyze/landcover", dependencies=[Depends(require_ee)])
async def analyze_landcover(request: Request):
    data = await request.json()
    try:
        return landcover_service.analyze_landcover(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Land cover error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/landcover-transition", dependencies=[Depends(require_ee)])
async def analyze_landcover_transition(request: Request):
    data = await request.json()
    try:
        return landcover_service.analyze_landcover_transition(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Land cover transition error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/landcover-change-map", dependencies=[Depends(require_ee)])
async def analyze_landcover_change_map(request: Request):
    data = await request.json()
    try:
        return landcover_service.analyze_landcover_change_map(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Land cover change map error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/landcover-hotspots", dependencies=[Depends(require_ee)])
async def analyze_landcover_hotspots(request: Request):
    """Ranked, vectorized pixel-change polygons (P0 hotspot detection) - see
    landcover_service.analyze_landcover_hotspots for the full contract."""
    data = await request.json()
    try:
        return landcover_service.analyze_landcover_hotspots(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Land cover hotspot error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
