"""Feature-authorized model catalog with scientific public metadata only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models.uploaded_model import UploadedModel
from app.core.viewer_features import require_feature
from app.db.session import get_db
from app.services.model_catalog_service import get_carbon_model, list_models as list_model_catalog

router = APIRouter(
    tags=["models"],
    dependencies=[Depends(require_feature("app.feature.carbon_stock"))],
)


@router.get("/models/list")
def models_list_legacy(model_type: str = "carbon", db: Session = Depends(get_db),
                       offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=100, ge=1, le=200)):
    """Legacy route backed by the same public catalog as the current endpoint."""
    return list_model_catalog(db, model_type=model_type, offset=offset, limit=limit)


@router.get("/models")
def models_list(
    model_type: str | None = None,
    include_inactive: bool = False,
    include_fallback: bool = Query(default=False),
    target_dataset: str | None = None,
    gee_deployable: bool | None = None,
    db: Session = Depends(get_db),
    offset: int = Query(default=0, ge=0, le=100000),
    limit: int = Query(default=100, ge=1, le=200),
):
    if include_inactive:
        raise HTTPException(status_code=403, detail="Inactive models are available only through the admin API")
    return list_model_catalog(
        db,
        model_type=model_type,
        target_dataset=target_dataset,
        gee_deployable=gee_deployable,
        include_fallback=include_fallback,
        offset=offset,
        limit=limit,
    )


@router.get("/models/{model_id}")
def model_detail(model_id: int, db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model or not model.is_active or model.lifecycle_state != "active" or (model.metadata_json or {}).get("format") != "onnx":
        raise HTTPException(status_code=404, detail="Model not found")
    return model.to_dict_public(include_path=False)


@router.get("/models/by-name/{model_name}")
def model_by_name(model_name: str, db: Session = Depends(get_db)):
    model = get_carbon_model(model_name, db)
    if model:
        return model
    raise HTTPException(status_code=404, detail="Model not found")
