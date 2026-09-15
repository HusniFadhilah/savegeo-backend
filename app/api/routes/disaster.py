"""Disaster monitoring endpoints - /api/disaster/*. Ported from backend/app.py.

Legacy does not gate `/sources` or `/bmkg-alerts` behind EE_INITIALIZED (they're
config reflection / an external XML feed) - only `dem-slope` actually touches
Earth Engine. All 3 remaining routes now additionally require a logged-in
disaster viewer (`get_current_disaster_viewer`) - public user or admin.
`event-map` is retained for the interactive on-demand before/after analysis
flow used by the Kalimantan 2026 fire analysis panel. Persisted published
event results continue to use `disaster_events.py`.
"""

from __future__ import annotations

import logging
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_ee
from app.core.security import get_current_disaster_viewer
from app.services import disaster_service, fire_multi_source_service
from app.services.gee_common import AnalysisError

router = APIRouter(prefix="/disaster", tags=["disaster"])
logger = logging.getLogger(__name__)


@router.post("/fire-multi-source", dependencies=[Depends(get_current_disaster_viewer)])
async def load_fire_multi_source(request: Request):
    try:
        return await run_in_threadpool(
            fire_multi_source_service.load_sources,
            await request.json(),
            bool(getattr(request.app.state, "ee_initialized", False)),
        )
    except (AnalysisError, ValueError) as exc:
        raise HTTPException(
            status_code=exc.status_code if isinstance(exc, AnalysisError) else 400, detail=str(exc)
        )


@router.post("/fire-big-boundaries", dependencies=[Depends(get_current_disaster_viewer)])
async def load_fire_big_boundaries(request: Request):
    try:
        return await run_in_threadpool(fire_multi_source_service.load_big_boundaries, await request.json())
    except (AnalysisError, ValueError) as exc:
        raise HTTPException(
            status_code=exc.status_code if isinstance(exc, AnalysisError) else 400, detail=str(exc)
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Layanan batas BIG belum dapat diakses")


@router.post("/fire-import", dependencies=[Depends(get_current_disaster_viewer)])
async def import_fire_observations(request: Request):
    try:
        return fire_multi_source_service.import_observations(await request.json())
    except (AnalysisError, ValueError) as exc:
        raise HTTPException(
            status_code=exc.status_code if isinstance(exc, AnalysisError) else 400, detail=str(exc)
        )


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


@router.post("/event-map", dependencies=[Depends(get_current_disaster_viewer), Depends(require_ee)])
async def get_disaster_event_map(request: Request):
    """Run before/after event analysis for the interactive disaster map.

    The fire path returns Sentinel-2 dNBR tiles and MODIS FireMask point
    detections in addition to the legacy flood/landslide response shape.
    """
    data = await request.json()
    try:
        return disaster_service.get_disaster_event_map(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post(
    "/fire-sam/jobs",
    status_code=202,
    dependencies=[Depends(get_current_disaster_viewer), Depends(require_ee)],
)
async def start_fire_sam_job(request: Request):
    """Segment candidate burn objects using MODIS thermal seeds + SamGeo."""
    try:
        return disaster_service.start_fire_sam_job(await request.json())
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/fire-sam/jobs/{job_id}", dependencies=[Depends(get_current_disaster_viewer)])
def get_fire_sam_job(job_id: str):
    try:
        return disaster_service.get_fire_sam_job(job_id)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
