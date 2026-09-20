"""Crop Monitoring endpoints. `/crop-monitoring/commodities` and
`/crop-monitoring/weather-providers` are static (no GEE), safe even if Earth
Engine is down. `/analyze/crop-monitoring` is GEE-dependent, gated behind
`require_ee`, and follows `vegetation.py`'s raw-JSON-body/raw-dict-return
convention (no Pydantic here - see `app/schemas/common.py`'s documented
split; Field CRUD in `fields.py` is the Pydantic-schema resource instead).
"""
from __future__ import annotations

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import get_current_app_viewer
from app.db.session import get_db
from app.registries.crop_registry import get_catalog_payload
from app.registries.weather_provider_registry import get_catalog_payload as get_weather_catalog_payload
from app.services import crop_monitoring_service
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(tags=["crop-monitoring"])
logger = logging.getLogger(__name__)


@router.get("/crop-monitoring/commodities")
def crop_monitoring_commodities():
    return get_catalog_payload()


@router.get("/crop-monitoring/weather-providers")
def crop_monitoring_weather_providers():
    return get_weather_catalog_payload()


@router.post("/analyze/crop-monitoring", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_crop_monitoring(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return crop_monitoring_service.run_crop_monitoring(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        logger.warning("Crop monitoring Earth Engine error: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Crop monitoring analysis error")
        raise HTTPException(status_code=500, detail="Crop monitoring analysis failed") from e
