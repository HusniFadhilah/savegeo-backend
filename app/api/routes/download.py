"""GeoTIFF download URL generation - /api/download/geotiff. Ported from backend/app.py."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.db.session import get_db
from app.services import download_service
from app.services.gee_common import AnalysisError

router = APIRouter(prefix="/download", tags=["download"])
logger = logging.getLogger(__name__)


@router.post("/geotiff", dependencies=[Depends(require_ee)])
async def download_geotiff(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return download_service.download_geotiff(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Download GeoTIFF error")
        raise HTTPException(status_code=500, detail=str(e))
