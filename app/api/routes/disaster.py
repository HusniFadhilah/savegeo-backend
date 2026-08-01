"""Disaster monitoring endpoints - /api/disaster/*. Ported from backend/app.py.

Legacy does not gate `/sources` or `/bmkg-alerts` behind EE_INITIALIZED (they're
config reflection / an external XML feed) - only `dem-slope` and `event-map`
actually touch Earth Engine.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import require_ee
from app.services import disaster_service
from app.services.gee_common import AnalysisError

router = APIRouter(prefix="/disaster", tags=["disaster"])
logger = logging.getLogger(__name__)


@router.get("/sources")
def get_disaster_sources():
    return disaster_service.get_disaster_sources()


@router.get("/bmkg-alerts")
def get_bmkg_alerts(limit: int = 30):
    try:
        return disaster_service.get_bmkg_alerts(limit=limit)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/dem-slope", dependencies=[Depends(require_ee)])
async def get_disaster_dem_slope(request: Request):
    data = await request.json()
    try:
        return disaster_service.get_disaster_dem_slope(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/event-map", dependencies=[Depends(require_ee)])
async def get_disaster_event_map(request: Request):
    data = await request.json()
    try:
        return disaster_service.get_disaster_event_map(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
