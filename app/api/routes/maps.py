from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.core.viewer_features import FEATURE_ACCESS_DENIED_DETAIL, ensure_feature_access, feature_code_for_module, viewer_feature_codes
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from app.registries.map_layer_registry import get_all_layers, get_basemaps

router = APIRouter(tags=["maps"])


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
