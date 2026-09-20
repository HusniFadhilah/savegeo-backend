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
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_ee
from app.core.security import get_current_disaster_viewer
from app.services import disaster_service, fire_multi_source_service, firms_service, wind_service
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(prefix="/disaster", tags=["disaster"])
logger = logging.getLogger(__name__)


@router.get("/firms/sources")
def get_firms_sources(viewer=Depends(get_current_disaster_viewer)):
    return firms_service.source_metadata()


@router.get("/firms/fires")
def get_firms_fires(
    source: str = Query(default="all"),
    day_range: int = Query(default=1),
    date: str | None = Query(default=None),
    west: float = Query(...),
    south: float = Query(...),
    east: float = Query(...),
    north: float = Query(...),
    min_confidence: str | None = Query(default=None),
    min_frp: float | None = Query(default=None),
    limit: int = Query(default=2000),
    viewer=Depends(get_current_disaster_viewer),
):
    try:
        return firms_service.get_fires(
            source=source,
            day_range=day_range,
            requested_date=date,
            bbox=(west, south, east, north),
            min_confidence=min_confidence,
            min_frp=min_frp,
            limit=limit,
        )
    except firms_service.FirmsRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


@router.get("/firms/wms")
def get_firms_wms(
    layer: str = Query(...),
    west: float = Query(...),
    south: float = Query(...),
    east: float = Query(...),
    north: float = Query(...),
    crs: str = Query(default="EPSG:4326"),
    width: int = Query(default=1024),
    height: int = Query(default=512),
    format: str = Query(default="image/png"),
    viewer=Depends(get_current_disaster_viewer),
):
    try:
        payload = firms_service.fetch_wms(
            layer=layer,
            bbox=(west, south, east, north),
            crs=crs,
            width=width,
            height=height,
            image_format=format,
        )
        return Response(content=payload["content"], media_type=payload["content_type"], headers={"X-Attribution": payload["attribution"]})
    except firms_service.FirmsRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


@router.get("/wind")
def get_wind(
    west: float = Query(...),
    south: float = Query(...),
    east: float = Query(...),
    north: float = Query(...),
    date: str | None = Query(default=None),
    viewer=Depends(get_current_disaster_viewer),
):
    try:
        return wind_service.get_wind((west, south, east, north), requested_date=date)
    except (wind_service.WindRequestError, ValueError) as exc:
        status_code = exc.status_code if isinstance(exc, wind_service.WindRequestError) else 400
        raise HTTPException(status_code=status_code, detail=public_analysis_error(exc)) from exc


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
            status_code=exc.status_code if isinstance(exc, AnalysisError) else 400, detail=public_analysis_error(exc)
        )


@router.post("/fire-big-boundaries", dependencies=[Depends(get_current_disaster_viewer)])
async def load_fire_big_boundaries(request: Request):
    try:
        return await run_in_threadpool(fire_multi_source_service.load_big_boundaries, await request.json())
    except (AnalysisError, ValueError) as exc:
        raise HTTPException(
            status_code=exc.status_code if isinstance(exc, AnalysisError) else 400, detail=public_analysis_error(exc)
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Layanan batas BIG belum dapat diakses")


@router.post("/fire-import", dependencies=[Depends(get_current_disaster_viewer)])
async def import_fire_observations(request: Request):
    try:
        return fire_multi_source_service.import_observations(await request.json())
    except (AnalysisError, ValueError) as exc:
        raise HTTPException(
            status_code=exc.status_code if isinstance(exc, AnalysisError) else 400, detail=public_analysis_error(exc)
        )


@router.get("/sources")
def get_disaster_sources(viewer=Depends(get_current_disaster_viewer)):
    return disaster_service.get_disaster_sources()


@router.get("/bmkg-alerts")
def get_bmkg_alerts(limit: int = 30, viewer=Depends(get_current_disaster_viewer)):
    try:
        return disaster_service.get_bmkg_alerts(limit=limit)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))


@router.post("/dem-slope", dependencies=[Depends(get_current_disaster_viewer), Depends(require_ee)])
async def get_disaster_dem_slope(request: Request):
    data = await request.json()
    try:
        return disaster_service.get_disaster_dem_slope(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))


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
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))


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
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))


@router.get("/fire-sam/jobs/{job_id}", dependencies=[Depends(get_current_disaster_viewer)])
def get_fire_sam_job(job_id: str):
    try:
        return disaster_service.get_fire_sam_job(job_id)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
