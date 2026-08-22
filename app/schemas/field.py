"""Pydantic request bodies for the public Field CRUD routes
(`app/api/routes/fields.py`). This is a plain CRUD resource (unlike the raw-
dict GEE analysis routes - see `app/schemas/common.py`'s documented split),
so it gets real schemas, mirroring `app/schemas/user.py`'s minimal style -
no extra validation beyond typing, business rules (unknown commodity, empty
geojson, etc.) are checked in the route/service.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class FieldCreateRequest(BaseModel):
    name: str
    geojson: dict
    commodity: str
    variety: str | None = None
    planting_date: dt.date | None = None
    season_label: str | None = None


class FieldUpdateRequest(BaseModel):
    name: str | None = None
    commodity: str | None = None
    variety: str | None = None
    planting_date: dt.date | None = None
    season_label: str | None = None
