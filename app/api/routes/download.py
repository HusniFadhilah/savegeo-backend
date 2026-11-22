"""GeoTIFF download URL generation - /api/download/geotiff. Ported from backend/app.py."""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import get_current_app_viewer
from app.core.bounded_json import read_typed_json_object
from app.core.viewer_features import ensure_export_feature_access
from app.db.session import get_db
from app.services import download_service
from app.schemas.analysis_jobs import AnalysisPayload
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(prefix="/download", tags=["download"])
logger = logging.getLogger(__name__)


class RasterTaskStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_token: str = Field(min_length=1, max_length=4096, pattern="^[^\\r\\n\\x00]+$")


@router.post("/raster-task-status", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
def raster_task_status(data: RasterTaskStatusRequest, viewer: object = Depends(get_current_app_viewer)):
    from app.services.raster_batch_export import export_status
    from app.api.routes.analysis_jobs import job_owner_key

    try:
        return export_status(data.task_token, job_owner_key(viewer))
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


@router.post("/geotiff", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def download_geotiff(
    request: Request,
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    data = await read_typed_json_object(request, AnalysisPayload)
    from app.api.routes.analysis_jobs import job_owner_key

    data["_request_owner"] = job_owner_key(viewer)
    ensure_export_feature_access(viewer, data, db)
    try:
        return await run_in_threadpool(download_service.download_geotiff, db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        logger.warning("GeoTIFF Earth Engine error: %s", e)
        raise HTTPException(status_code=422, detail="Earth Engine tidak dapat memproses AOI atau parameter ekspor.") from e
    except Exception as e:
        logger.exception("Download GeoTIFF error")
        raise HTTPException(status_code=500, detail="Ekspor GeoTIFF gagal. Coba lagi atau hubungi administrator.") from e
