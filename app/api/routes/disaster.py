"""Disaster monitoring endpoints - /api/disaster/*. Ported from backend/app.py.

Legacy does not gate `/sources` or `/bmkg-alerts` behind EE_INITIALIZED (they're
config reflection / an external XML feed) - only `dem-slope` actually touches
Earth Engine. All 3 remaining routes now additionally require a logged-in
disaster viewer (`get_current_disaster_viewer`) - public user or admin.
`event-map` was removed here (superseded by the new
model-based analysis run flow in `disaster_events.py`); the underlying
`disaster_service.get_disaster_event_map` function itself is left alone
(dead code, intentionally not refactored further).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import require_ee
from app.core.security import get_current_disaster_viewer
from app.services import disaster_service
from app.services.gee_common import AnalysisError

router = APIRouter(prefix="/disaster", tags=["disaster"])
logger = logging.getLogger(__name__)


@router.get("/sources")
def get_disaster_sources(viewer=Depends(get_current_disaster_viewer)):
    return disaster_service.get_disaster_sources()


@router.get("/bmkg-alerts")
def get_bmkg_alerts(limit: int = 30, viewer=Depends(get_current_disaster_viewer)):
    try:
        return disaster_service.get_bmkg_alerts(limit=limit)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/dem-slope", dependencies=[Depends(get_current_disaster_viewer), Depends(require_ee)])
async def get_disaster_dem_slope(request: Request):
    data = await request.json()
    try:
        return disaster_service.get_disaster_dem_slope(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
