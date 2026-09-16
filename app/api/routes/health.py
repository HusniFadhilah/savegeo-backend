from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db.models.gee_credential import GEECredential
from app.db.models.uploaded_model import UploadedModel
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    ee_initialized = getattr(request.app.state, "ee_initialized", False)
    active_cred = db.query(GEECredential).filter_by(is_active=True).first()
    active_models = db.query(UploadedModel).filter_by(is_active=True).count()
    return {
        "status": "ok",
        "ee_initialized": ee_initialized,
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        # Never expose service-account identity in a public liveness response.
        "active_credential": bool(active_cred),
        "active_credential_configured": bool(active_cred),
        "active_models": active_models,
    }
