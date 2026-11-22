from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models.gee_credential import GEECredential
from app.db.models.uploaded_model import UploadedModel
from app.db.session import get_db
from app.core.temporal import format_rfc3339, utc_now
from app.core.version import get_public_version_metadata

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/live")
def liveness():
    return {"status": "ok"}


@router.get("/ready")
def readiness(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ready"}


@router.get("/version")
def version() -> dict[str, object]:
    """Return only public build/deployment provenance; never configuration or secrets."""
    return get_public_version_metadata()


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    ee_initialized = getattr(request.app.state, "ee_initialized", False)
    database_connected = True
    model_catalog_available = True
    model_lifecycle_schema_current = True
    active_cred = None
    active_models = 0
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        database_connected = False
        model_catalog_available = False
        logger.warning("Health check database connection failed: %s", type(exc).__name__)

    if database_connected:
        try:
            active_cred = db.query(GEECredential).filter_by(is_active=True).first()
        except SQLAlchemyError as exc:
            logger.warning("Health check credential catalog probe failed: %s", type(exc).__name__)

        try:
            model_columns = {column["name"] for column in sa_inspect(db.get_bind()).get_columns("uploaded_models")}
            if "lifecycle_state" in model_columns:
                active_models = db.query(UploadedModel).filter_by(is_active=True, lifecycle_state="active").count()
            else:
                model_lifecycle_schema_current = False
                active_models = db.query(UploadedModel).filter_by(is_active=True).count()
        except SQLAlchemyError as exc:
            model_catalog_available = False
            logger.warning("Health check model catalog probe failed: %s", type(exc).__name__)

    return {
        "status": "ok" if database_connected and model_catalog_available and model_lifecycle_schema_current else "degraded",
        "ee_initialized": ee_initialized,
        "database_connected": database_connected,
        "active_model_catalog_available": model_catalog_available,
        "model_lifecycle_schema_current": model_lifecycle_schema_current,
        "timestamp": format_rfc3339(utc_now()),
        # Never expose service-account identity in a public liveness response.
        "active_credential": bool(active_cred),
        "active_credential_configured": bool(active_cred),
        "active_models": active_models,
    }
