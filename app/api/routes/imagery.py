"""Raw satellite imagery browser - /api/imagery/scenes (list real scenes with
their exact acquisition date+time) and /api/imagery/scene-tile (RGB tile for
one single scene, no compositing) - independent of vegetation/landcover/
carbon analysis. See app.services.imagery_service for the full rationale.
"""
from __future__ import annotations

import ee
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.db.session import get_db
from app.services import imagery_service
from app.services.gee_common import AnalysisError

router = APIRouter(tags=["imagery"])
logger = logging.getLogger(__name__)


@router.post("/imagery/scenes", dependencies=[Depends(require_ee)])
async def imagery_scenes(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return imagery_service.list_scenes(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery scenes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/imagery/scene-tile", dependencies=[Depends(require_ee)])
async def imagery_scene_tile(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        return imagery_service.get_scene_tile(db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Imagery scene-tile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
