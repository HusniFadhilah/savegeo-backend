"""Owner-scoped reads and writes for Crop Monitoring boundaries."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.db.models.field import Field
from app.registries.crop_registry import estimate_harvest_days


def _compute_estimated_harvest_date(commodity: str, planting_date: dt.date | None) -> dt.date | None:
    if planting_date is None:
        return None
    duration = estimate_harvest_days(commodity)
    if duration is None:
        return None
    return planting_date + dt.timedelta(days=duration)


def list_fields(db: Session, owner_key: str) -> list[Field]:
    if not owner_key:
        return []
    return db.query(Field).filter(Field.owner_key == owner_key).order_by(Field.created_at.desc()).all()


def get_field(db: Session, field_id: int, owner_key: str) -> Field | None:
    if not owner_key:
        return None
    return db.query(Field).filter(Field.id == field_id, Field.owner_key == owner_key).first()


def create_field(db: Session, data: dict, owner_key: str) -> Field:
    if not owner_key:
        raise ValueError("Field owner is required")
    commodity = data["commodity"]
    planting_date = data.get("planting_date")
    field = Field(
        owner_key=owner_key,
        name=data["name"],
        geojson=data["geojson"],
        area_ha=data["area_ha"],
        commodity=commodity,
        variety=data.get("variety"),
        planting_date=planting_date,
        season_label=data.get("season_label"),
        estimated_harvest_date=_compute_estimated_harvest_date(commodity, planting_date),
    )
    db.add(field)
    db.commit()
    db.refresh(field)
    return field


def update_field(db: Session, field: Field, data: dict) -> Field:
    for key in ("name", "variety", "season_label"):
        if key in data:
            setattr(field, key, data[key])
    commodity_changed = "commodity" in data and data["commodity"] != field.commodity
    planting_changed = "planting_date" in data and data["planting_date"] != field.planting_date
    if "commodity" in data:
        field.commodity = data["commodity"]
    if "planting_date" in data:
        field.planting_date = data["planting_date"]
    if commodity_changed or planting_changed:
        field.estimated_harvest_date = _compute_estimated_harvest_date(field.commodity, field.planting_date)
    db.commit()
    db.refresh(field)
    return field


def delete_field(db: Session, field: Field) -> None:
    db.delete(field)
    db.commit()
