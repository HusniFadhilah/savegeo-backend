"""Carbon analysis endpoints - /api/analyze/carbon*. Ported from backend/app.py."""
from __future__ import annotations

import logging

import ee
import requests
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.db.session import get_db
from app.services import carbon_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["carbon"])
logger = logging.getLogger(__name__)
UPSTREAM_CONNECTION_ERROR = (
    "Koneksi ke layanan data eksternal terputus sebelum respons diterima. "
    "Coba jalankan ulang analisis; jika berulang, kecilkan AOI/rentang waktu "
    "atau cek koneksi ke Earth Engine/penyedia raster."
)


@router.post("/analyze/carbon", dependencies=[Depends(require_ee)])
def analyze_carbon(data: dict = Body(...), db: Session = Depends(get_db)):
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    try:
        return carbon_service.analyze_carbon(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.warning(f"Carbon analysis validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except requests.RequestException as e:
        logger.warning("Carbon upstream connection error: %s", e)
        raise HTTPException(status_code=503, detail=f"{UPSTREAM_CONNECTION_ERROR} Detail teknis: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Carbon analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/carbon-local")
def analyze_carbon_local(data: dict = Body(...), db: Session = Depends(get_db)):
    """Non-GEE carbon estimation path — does NOT require Earth Engine."""
    try:
        return carbon_service.analyze_carbon_local(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.warning(f"Non-GEE carbon analysis validation error: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except requests.RequestException as e:
        logger.warning("Non-GEE carbon upstream connection error: %s", e)
        raise HTTPException(status_code=503, detail=f"{UPSTREAM_CONNECTION_ERROR} Detail teknis: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Non-GEE carbon analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/carbon-delta", dependencies=[Depends(require_ee)])
def analyze_carbon_delta(data: dict = Body(...), db: Session = Depends(get_db)):
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    try:
        return carbon_service.analyze_carbon_delta(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.warning(f"Carbon delta validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except ee.EEException as e:
        # Malformed AOI / geometry input reaching Earth Engine (e.g. a bad
        # GeoJSON shape from the client) is a client error, not a server fault -
        # EEException is not a ValueError subclass so it needs its own branch,
        # otherwise it falls through to the generic 500 handler below.
        raise HTTPException(status_code=400, detail=str(e))
    except requests.RequestException as e:
        logger.warning("Carbon delta upstream connection error: %s", e)
        raise HTTPException(status_code=503, detail=f"{UPSTREAM_CONNECTION_ERROR} Detail teknis: {e}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Carbon delta error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
