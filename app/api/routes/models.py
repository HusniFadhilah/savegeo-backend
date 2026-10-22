"""Public model registry read endpoints (no auth) — /api/models*."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models.uploaded_model import UploadedModel
from app.core.viewer_features import require_feature
from app.db.session import get_db
from app.registries.model_compatibility import backfill_legacy_metadata, get_compatible_datasets
from app.registries.carbon_dataset_registry import canonical_carbon_dataset_key
from app.registries.carbon_pool_models import builtin_carbon_models, get_builtin_carbon_model
from app.registries.granite_biomass_registry import granite_biomass_model

router = APIRouter(
    tags=["models"],
    dependencies=[Depends(require_feature("app.feature.carbon_stock"))],
)


@router.get("/models/list")
def models_list_legacy(model_type: str = "carbon", db: Session = Depends(get_db)):
    """Legacy alias — returns full (non-public) dict shape, matches old `/api/models/list`."""
    models = db.query(UploadedModel).filter_by(model_type=model_type).all()
    return {"models": [m.to_dict() for m in models], "count": len(models)}


@router.get("/models")
def models_list(
    model_type: str | None = None,
    include_inactive: bool = False,
    target_dataset: str | None = None,
    gee_deployable: bool | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(UploadedModel)
    if model_type:
        query = query.filter_by(model_type=model_type)
    if not include_inactive:
        query = query.filter_by(is_active=True)
    models = query.all()

    results = []
    for m in models:
        meta = backfill_legacy_metadata(m.metadata_json, m.name)
        if target_dataset and canonical_carbon_dataset_key(target_dataset) not in get_compatible_datasets(meta):
            continue
        if gee_deployable is not None and bool(meta.get("gee_deployable")) != gee_deployable:
            continue
        results.append(m.to_dict_public())

    if model_type in (None, "carbon") and gee_deployable is not True:
        granite_model = granite_biomass_model()
        if granite_model:
            existing_names = {m["name"] for m in results}
            requested_key = canonical_carbon_dataset_key(target_dataset) if target_dataset else None
            compatible = granite_model["metadata_json"]["compatible_reference_datasets"]
            if (
                granite_model["name"] not in existing_names
                and (requested_key is None or requested_key in compatible)
                and (gee_deployable is None or not granite_model["metadata_json"].get("gee_deployable"))
            ):
                results.append(granite_model)

    if model_type in (None, "carbon") and gee_deployable is not False:
        existing_names = {m["name"] for m in results}
        results.extend(m for m in builtin_carbon_models(target_dataset, db=db) if m["name"] not in existing_names)
    return {"models": results, "count": len(results)}


@router.get("/models/{model_id}")
def model_detail(model_id: int, db: Session = Depends(get_db)):
    model = db.get(UploadedModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model.to_dict_public(include_path=False)


@router.get("/models/by-name/{model_name}")
def model_by_name(model_name: str, db: Session = Depends(get_db)):
    granite_model = granite_biomass_model()
    if granite_model and granite_model["name"] == model_name:
        return granite_model
    builtin = get_builtin_carbon_model(model_name, db=db)
    if builtin:
        return builtin
    model = db.query(UploadedModel).filter_by(name=model_name, is_active=True).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model.to_dict_public(include_path=False)
