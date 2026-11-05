from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.core.viewer_features import FEATURE_ACCESS_DENIED_DETAIL, ensure_feature_access, feature_code_for_module, viewer_feature_codes
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from app.registries.map_layer_registry import get_all_layers, get_basemaps
from app.services import apple_mapkit_service

router = APIRouter(tags=["maps"])
logger = logging.getLogger(__name__)


@router.get("/map-layers")
def map_layers(
    module: str | None = None,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    layers = get_all_layers(module=module)
    if module is not None:
        code = feature_code_for_module(module)
        if code is None:
            raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
        ensure_feature_access(viewer, code, db)
    else:
        allowed = viewer_feature_codes(db, viewer)
        layers = [
            layer
            for layer in layers
            if layer.get("module") is None
            or (code := feature_code_for_module(layer.get("module"))) is not None and code in allowed
        ]
    return {"layers": layers, "count": len(layers)}


@router.get("/basemaps")
def basemaps(db: Session = Depends(get_db)):
    layers = get_basemaps(db=db)
    return {"layers": layers, "count": len(layers)}


@router.get("/maps/apple-mapkit/token")
def apple_mapkit_token(request: Request, _viewer: AdminUser | User = Depends(get_current_app_viewer)):
    """Issue a short-lived MapKit JS token for the Apple satellite basemap."""
    if not apple_mapkit_service.is_configured():
        raise HTTPException(status_code=503, detail="Apple MapKit belum dikonfigurasi di backend")
    origin = apple_mapkit_service.token_origin(request.headers.get("origin"), request.headers.get("referer"))
    try:
        token, expires_at = apple_mapkit_service.create_token(origin)
    except Exception as exc:  # noqa: BLE001 - never echo key material or parser errors
        logger.warning("Apple MapKit token signing failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Apple MapKit tidak tersedia; periksa konfigurasi kunci") from exc
    return {"token": token, "expires_at": expires_at, "origin": origin}
