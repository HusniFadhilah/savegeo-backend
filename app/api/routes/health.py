from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models.gee_credential import GEECredential
from app.db.models.uploaded_model import UploadedModel
from app.db.session import get_db
from app.core.temporal import format_rfc3339, utc_now
from app.core.version import get_public_version_metadata

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/version")
def version() -> dict[str, object]:
    """Return only public build/deployment provenance; never configuration or secrets."""
    return get_public_version_metadata()


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    ee_initialized = getattr(request.app.state, "ee_initialized", False)
    database_connected = True
    active_cred = None
    active_models = 0
    try:
        active_cred = db.query(GEECredential).filter_by(is_active=True).first()
        active_models = db.query(UploadedModel).filter_by(is_active=True).count()
    except SQLAlchemyError as exc:
        database_connected = False
        logger.warning("Health check database probe failed: %s", type(exc).__name__)
    return {
        "status": "ok" if database_connected else "degraded",
        "ee_initialized": ee_initialized,
        "database_connected": database_connected,
        "timestamp": format_rfc3339(utc_now()),
        # Never expose service-account identity in a public liveness response.
        "active_credential": bool(active_cred),
        "active_credential_configured": bool(active_cred),
        "active_models": active_models,
    }
