"""Single public catalog for built-in, approved, and optional carbon models."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models.uploaded_model import UploadedModel
from app.registries.carbon_dataset_registry import canonical_carbon_dataset_key
from app.registries.carbon_pool_models import builtin_carbon_models
from app.registries.granite_biomass_registry import granite_biomass_model
from app.registries.model_compatibility import backfill_legacy_metadata, get_compatible_datasets


def list_carbon_models(
    db: Session,
    *,
    target_dataset: str | None = None,
    gee_deployable: bool | None = None,
    include_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Combine model sources without deleting or mutating their legacy records."""
    requested_key = canonical_carbon_dataset_key(target_dataset) if target_dataset else None
    results: list[dict[str, Any]] = []
    names: set[str] = set()

    uploaded = (
        db.query(UploadedModel)
        .filter_by(model_type="carbon", is_active=True, lifecycle_state="active")
        .filter(UploadedModel.metadata_json["format"].as_string() == "onnx")
        .order_by(UploadedModel.id)
        .all()
    )
    for row in uploaded:
        model = row.to_dict_public(include_path=False)
        metadata = backfill_legacy_metadata(model.get("metadata_json"), model.get("name", ""))
        model["metadata_json"] = metadata
        if requested_key and requested_key not in get_compatible_datasets(metadata):
            continue
        if gee_deployable is not None and bool(metadata.get("gee_deployable")) != gee_deployable:
            continue
        if model["name"] not in names:
            results.append(model)
            names.add(model["name"])

    if gee_deployable is not True:
        granite = granite_biomass_model()
        if granite and (requested_key is None or requested_key in granite["metadata_json"].get("compatible_reference_datasets", [])):
            if granite["name"] not in names:
                results.append(granite)
                names.add(granite["name"])

    if gee_deployable is not False:
        for model in builtin_carbon_models(target_dataset, db=db, include_fallback=include_fallback):
            metadata = model.get("metadata_json") or {}
            if requested_key and requested_key not in get_compatible_datasets(metadata):
                continue
            if gee_deployable is not None and bool(metadata.get("gee_deployable")) != gee_deployable:
                continue
            if model["name"] not in names:
                results.append(model)
                names.add(model["name"])

    return results


def list_models(
    db: Session,
    *,
    model_type: str | None = None,
    target_dataset: str | None = None,
    gee_deployable: bool | None = None,
    include_fallback: bool = False,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    if model_type in (None, "carbon"):
        all_models = list_carbon_models(
            db,
            target_dataset=target_dataset,
            gee_deployable=gee_deployable,
            include_fallback=include_fallback,
        )
    else:
        rows = (
            db.query(UploadedModel)
            .filter_by(model_type=model_type, is_active=True, lifecycle_state="active")
            .filter(UploadedModel.metadata_json["format"].as_string() == "onnx")
            .order_by(UploadedModel.id)
            .all()
        )
        all_models = [row.to_dict_public(include_path=False) for row in rows]
    page = all_models[offset : offset + limit]
    return {
        "models": page,
        "count": len(page),
        "total": len(all_models),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < len(all_models),
    }


def get_carbon_model(name: str, db: Session) -> dict[str, Any] | None:
    return next((model for model in list_carbon_models(db) if model.get("name") == name), None)
