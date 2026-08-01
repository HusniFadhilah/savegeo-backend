"""Dataset discovery endpoints: /api/landcover/datasets, /api/datasets, /api/carbon/datasets."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.registries.landcover_dataset_registry import (
    LAND_COVER_DATASET_OPTIONS,
    LAND_COVER_LEGENDS,
    LAND_COVER_NATIVE_SCALE,
    get_dataset_list,
)
from app.services.carbon_service import get_carbon_dataset_list

router = APIRouter(tags=["datasets"])


@router.get("/landcover/datasets")
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
    module: Optional[str] = None,
    provider: Optional[str] = None,
    include_unavailable: bool = False,
    db: Session = Depends(get_db),
):
    if module == "carbon":
        datasets = get_carbon_dataset_list(db, provider=provider, require_model=not include_unavailable)
    else:
        datasets = get_dataset_list(module=module, provider=provider)
    return {"datasets": datasets, "count": len(datasets)}


@router.get("/carbon/datasets")
def list_carbon_datasets(
    provider: Optional[str] = None,
    include_unavailable: bool = False,
    db: Session = Depends(get_db),
):
    datasets = get_carbon_dataset_list(db, provider=provider, require_model=not include_unavailable)
    return {"datasets": datasets, "count": len(datasets)}
