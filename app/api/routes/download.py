"""GeoTIFF download URL generation - /api/download/geotiff. Ported from backend/app.py."""
from __future__ import annotations

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import get_current_app_viewer
from app.db.session import get_db
from app.services import download_service
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(prefix="/download", tags=["download"])
logger = logging.getLogger(__name__)


@router.post("/geotiff", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def download_geotiff(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return download_service.download_geotiff(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        logger.warning("GeoTIFF Earth Engine error: %s", e)
        raise HTTPException(status_code=422, detail="Earth Engine tidak dapat memproses AOI atau parameter ekspor.") from e
    except Exception as e:
        logger.exception("Download GeoTIFF error")
        raise HTTPException(status_code=500, detail="Ekspor GeoTIFF gagal. Coba lagi atau hubungi administrator.") from e
