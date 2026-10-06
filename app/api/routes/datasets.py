"""Dataset discovery endpoints: /api/landcover/datasets, /api/datasets, /api/carbon/datasets."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.core.viewer_features import FEATURE_ACCESS_DENIED_DETAIL, FEATURE_CODES, ensure_feature_access, feature_code_for_module, require_feature
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from app.registries.landcover_dataset_registry import (
    LAND_COVER_DATASET_OPTIONS,
    LAND_COVER_LEGENDS,
    LAND_COVER_NATIVE_SCALE,
    get_dataset_list,
)
from app.services.carbon_service import get_carbon_dataset_list

router = APIRouter(tags=["datasets"])


@router.get("/landcover/datasets", dependencies=[Depends(require_feature("app.feature.land_cover"))])
def list_landcover_datasets():
    return {
        key: {
            **info,
            "key": key,
            "native_scale": LAND_COVER_NATIVE_SCALE.get(key),
            "class_count": len(LAND_COVER_LEGENDS.get(key, {})),
        }
        for key, info in LAND_COVER_DATASET_OPTIONS.items()
    }


@router.get("/datasets")
def list_datasets(
    module: str | None = None,
    provider: str | None = None,
    include_unavailable: bool = False,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    requested_module = module.strip().casefold() if isinstance(module, str) else "landcover"
    code = feature_code_for_module(requested_module)
    if code not in {FEATURE_CODES["carbon_stock"], FEATURE_CODES["land_cover"]}:
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
    ensure_feature_access(viewer, code, db)
    if code == FEATURE_CODES["carbon_stock"]:
        datasets = get_carbon_dataset_list(db, provider=provider, require_model=not include_unavailable)
    else:
        datasets = get_dataset_list(module="landcover", provider=provider)
    return {"datasets": datasets, "count": len(datasets)}


@router.get("/carbon/datasets", dependencies=[Depends(require_feature("app.feature.carbon_stock"))])
def list_carbon_datasets(
    provider: str | None = None,
    include_unavailable: bool = False,
    db: Session = Depends(get_db),
):
    datasets = get_carbon_dataset_list(db, provider=provider, require_model=not include_unavailable)
    return {"datasets": datasets, "count": len(datasets)}
