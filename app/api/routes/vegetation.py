"""Vegetation endpoints.

`/api/vegetation/catalog` is static (no GEE) and safe even if Earth Engine is down —
per the legacy registry's own docstring. `/api/analyze/vegetation*` are GEE-dependent
and gated behind `require_ee`. Ported from backend/app.py.
"""
from __future__ import annotations

import ee
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.db.session import get_db
from app.registries.vegetation_index_registry import get_catalog_payload
from app.services import vegetation_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["vegetation"])
logger = logging.getLogger(__name__)


@router.get("/vegetation/catalog")
def vegetation_catalog():
    return get_catalog_payload()


@router.post("/analyze/vegetation", dependencies=[Depends(require_ee)])
async def analyze_vegetation(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return vegetation_service.analyze_vegetation(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Vegetation analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/vegetation/compare", dependencies=[Depends(require_ee)])
async def analyze_vegetation_compare(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return vegetation_service.analyze_vegetation_compare(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Vegetation compare error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
