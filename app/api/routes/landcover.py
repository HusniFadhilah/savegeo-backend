"""Land cover analysis endpoints - /api/analyze/landcover*. Ported from backend/app.py."""
from __future__ import annotations

import logging

import ee
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import require_ee
from app.core.security import get_current_app_viewer
from app.services import landcover_service
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(tags=["landcover"])
logger = logging.getLogger(__name__)


@router.get("/landcover/reference-layers/{dataset}", dependencies=[Depends(get_current_app_viewer)])
async def direct_landcover_reference_layer(
    dataset: str,
    request: Request,
    year: int = Query(default=2024),
    start_month: int = Query(default=1),
    end_month: int = Query(default=12),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
):
    """Return a global LULC tile layer without AOI statistics."""
    try:
        from app.registries.landcover_dataset_registry import LAND_COVER_DATASET_OPTIONS
        meta = LAND_COVER_DATASET_OPTIONS.get(dataset, {})
        needs_ee = not (meta.get("provider_type", "").startswith("arcgis") and not meta.get("gee_id"))
        if needs_ee and not getattr(request.app.state, "ee_initialized", False):
            raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi untuk dataset ini.")
        return landcover_service.get_direct_landcover_reference_layer(
            dataset, year, start_month, end_month, start_date, end_date
        )
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc
    except HTTPException:
        raise
    except ee.EEException as exc:
        logger.warning("Direct land cover Earth Engine error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Earth Engine temporarily unavailable") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Direct land cover layer error")
        raise HTTPException(status_code=500, detail="Unable to load land cover layer") from exc


@router.post("/analyze/landcover", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_landcover(request: Request):
    data = await request.json()
    try:
        return landcover_service.analyze_landcover(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Land cover error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again")


@router.post("/analyze/landcover-transition", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_landcover_transition(request: Request):
    data = await request.json()
    try:
        return landcover_service.analyze_landcover_transition(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Land cover transition error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again")


@router.post("/analyze/landcover-change-map", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_landcover_change_map(request: Request):
    data = await request.json()
    try:
        return landcover_service.analyze_landcover_change_map(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Land cover change map error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again")


@router.post("/analyze/landcover-identify", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def identify_landcover(request: Request):
    data = await request.json()
    try:
        return landcover_service.identify_landcover_point(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException:
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Land cover identify error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again")


@router.post("/analyze/landcover-hotspots", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_landcover_hotspots(request: Request):
    """Ranked, vectorized pixel-change polygons (P0 hotspot detection) - see
    landcover_service.analyze_landcover_hotspots for the full contract."""
    data = await request.json()
    try:
        return landcover_service.analyze_landcover_hotspots(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException:
        raise HTTPException(status_code=400, detail="Earth Engine rejected the analysis parameters")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid analysis parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Land cover hotspot error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Analysis failed. Please try again")
