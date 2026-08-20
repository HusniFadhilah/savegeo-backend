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
from app.registries.satellite_provider_registry import DEFAULT_SATELLITE
from app.repositories.satellite_provider_repo import list_satellites
from app.services import vegetation_service
from app.services.gee_common import AnalysisError, CLOUD_MASK_TECHNIQUE_INFO, DEFAULT_CLOUD_MASK_TECHNIQUE

router = APIRouter(tags=["vegetation"])
logger = logging.getLogger(__name__)


@router.get("/vegetation/catalog")
def vegetation_catalog():
    return get_catalog_payload()


@router.get("/vegetation/satellites")
def vegetation_satellites(db: Session = Depends(get_db)):
    """Catalog of selectable satellite imagery providers - resolution/revisit/
    spec metadata for the frontend's satellite picker. GEE collection ids stay
    hardcoded (algorithmic); display metadata + is_active come from the
    `satellite_providers` DB overlay when present (see satellite_provider_repo.py),
    falling back to the static registry otherwise - no DB dependency to boot."""
    return {"satellites": list_satellites(db), "default": DEFAULT_SATELLITE}


@router.get("/vegetation/cloud-mask-techniques")
def vegetation_cloud_mask_techniques():
    """Static (no GEE) catalog of selectable Sentinel-2 cloud-masking
    techniques (SCL/QA60/s2cloudless) - see gee_common.build_s2_cloud_masked_collection."""
    return {"techniques": CLOUD_MASK_TECHNIQUE_INFO, "default": DEFAULT_CLOUD_MASK_TECHNIQUE}


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


@router.post("/analyze/vegetation/change-hotspots", dependencies=[Depends(require_ee)])
async def analyze_vegetation_change_hotspots(request: Request, db: Session = Depends(get_db)):
    """Ranked, vectorized vegetation-index change polygons (P0 hotspot detection,
    Geo-AI Assistant's `find_hotspots(metric="vegetation_change")`) - see
    vegetation_service.analyze_vegetation_change_hotspots for the full contract."""
    data = await request.json()
    try:
        return vegetation_service.analyze_vegetation_change_hotspots(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Vegetation change hotspot error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
