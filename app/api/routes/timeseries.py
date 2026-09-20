"""Vegetation index monthly time series - /api/timeseries. Ported from backend/app.py."""
from __future__ import annotations

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import get_current_app_viewer
from app.db.session import get_db
from app.services import vegetation_service
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(tags=["timeseries"])
logger = logging.getLogger(__name__)


@router.post("/timeseries", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_timeseries(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return vegetation_service.analyze_timeseries(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        logger.warning("Time series Earth Engine error: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Time series analysis error")
        raise HTTPException(status_code=500, detail="Time series analysis failed") from e
