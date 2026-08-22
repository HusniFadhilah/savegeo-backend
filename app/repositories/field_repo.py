"""Read/write repository for the `fields` table (Crop Monitoring). Shared,
no-owner list - see `app.db.models.field.Field`'s docstring for why there's
no ownership check here (unlike `disaster_repo`'s publish/ownership gates).
"""
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


def list_fields(db: Session) -> list[Field]:
    return db.query(Field).order_by(Field.created_at.desc()).all()


def get_field(db: Session, field_id: int) -> Field | None:
    return db.get(Field, field_id)


def create_field(db: Session, data: dict) -> Field:
    commodity = data["commodity"]
    planting_date = data.get("planting_date")
    field = Field(
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
