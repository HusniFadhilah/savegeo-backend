"""Public Field CRUD - /api/fields*. Crop Monitoring's Field is a shared,
no-login entity (Sidebar tab, not a gated route - see `Field` model
docstring), so every route here is public, same auth posture as
`companies.py`. Area is computed with `geo_utils.estimate_area_ha` (pure
Python shoelace formula, no GEE/`require_ee` needed - same helper
`disaster_aois` uses), not an Earth Engine geometry call.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session
from fastapi import Depends

from app.db.session import get_db
from app.registries.crop_registry import get_commodity
from app.repositories import field_repo
from app.schemas.field import FieldCreateRequest, FieldUpdateRequest
from app.services.geo_utils import estimate_area_ha

router = APIRouter(prefix="/fields", tags=["fields"])


def _validate_commodity(commodity: str) -> None:
    if get_commodity(commodity) is None:
        raise HTTPException(status_code=400, detail=f"Komoditas '{commodity}' tidak dikenal")


@router.post("")
def create_field(payload: FieldCreateRequest, db: Session = Depends(get_db)):
    _validate_commodity(payload.commodity)
    area_ha = estimate_area_ha(payload.geojson)
    if area_ha <= 0:
        raise HTTPException(status_code=400, detail="Geometri field tidak valid atau area = 0")
    field = field_repo.create_field(
        db,
        {
            "name": payload.name,
            "geojson": payload.geojson,
            "area_ha": area_ha,
            "commodity": payload.commodity,
            "variety": payload.variety,
            "planting_date": payload.planting_date,
            "season_label": payload.season_label,
        },
    )
    return field.to_dict(include_geojson=True)


@router.get("")
def list_fields(db: Session = Depends(get_db)):
    fields = field_repo.list_fields(db)
    return {"fields": [f.to_dict() for f in fields], "count": len(fields)}


@router.get("/{field_id}")
def get_field(field_id: int, db: Session = Depends(get_db)):
    field = field_repo.get_field(db, field_id)
    if field is None:
        raise HTTPException(status_code=404, detail="Field tidak ditemukan")
    return field.to_dict(include_geojson=True)


@router.patch("/{field_id}")
def update_field(field_id: int, payload: FieldUpdateRequest, db: Session = Depends(get_db)):
    field = field_repo.get_field(db, field_id)
    if field is None:
        raise HTTPException(status_code=404, detail="Field tidak ditemukan")
    data = payload.model_dump(exclude_unset=True)
    if "commodity" in data:
        _validate_commodity(data["commodity"])
    field = field_repo.update_field(db, field, data)
    return field.to_dict(include_geojson=True)


@router.delete("/{field_id}")
def delete_field(field_id: int, db: Session = Depends(get_db)):
    field = field_repo.get_field(db, field_id)
    if field is None:
        raise HTTPException(status_code=404, detail="Field tidak ditemukan")
    field_repo.delete_field(db, field)
    return {"deleted": True, "id": field_id}
