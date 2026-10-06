"""Application feature access currently granted to the signed-in principal."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.core.viewer_features import VIEWER_FEATURES, viewer_feature_codes
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(tags=["application-access"])


@router.get("/app/features")
def get_application_features(
    principal: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    allowed = viewer_feature_codes(db, principal)
    return {
        "features": [
            {"code": code, "name": name, "description": description, "enabled": code in allowed}
            for code, name, description in VIEWER_FEATURES
        ]
    }
