"""Carbon analysis endpoints - /api/analyze/carbon*. Ported from backend/app.py."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.db.session import get_db
from app.services import carbon_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["carbon"])
logger = logging.getLogger(__name__)


@router.post("/analyze/carbon", dependencies=[Depends(require_ee)])
async def analyze_carbon(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    try:
        return carbon_service.analyze_carbon(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.warning(f"Carbon analysis validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Carbon analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/carbon-local")
async def analyze_carbon_local(request: Request, db: Session = Depends(get_db)):
    """Non-GEE carbon estimation path — does NOT require Earth Engine."""
    data = await request.json()
    try:
        return carbon_service.analyze_carbon_local(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.warning(f"Non-GEE carbon analysis validation error: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Non-GEE carbon analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/carbon-delta", dependencies=[Depends(require_ee)])
async def analyze_carbon_delta(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    try:
        return carbon_service.analyze_carbon_delta(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.warning(f"Carbon delta validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Carbon delta error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
