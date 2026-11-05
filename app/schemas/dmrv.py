"""Pydantic schemas for the dMRV project API."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_Strict):
    name: str = Field(min_length=1, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    activity_type: str = Field(default="ARR", max_length=32)
    jurisdiction: str | None = Field(default=None, max_length=128)
    methodology_id: str | None = Field(default=None, max_length=64)
    methodology_version: str | None = Field(default=None, max_length=32)
    start_date: dt.date | None = None
    crediting_period_years: int | None = Field(default=None, ge=1, le=100)


class ProjectUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    jurisdiction: str | None = Field(default=None, max_length=128)
    methodology_id: str | None = Field(default=None, max_length=64)
    methodology_version: str | None = Field(default=None, max_length=32)
    start_date: dt.date | None = None
    crediting_period_years: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _name_cannot_be_cleared(self):
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name tidak boleh dikosongkan")
        return self


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    organization: str | None
    activity_type: str
    jurisdiction: str | None
    methodology_id: str | None
    methodology_version: str | None
    start_date: dt.date | None
    crediting_period_years: int | None
    status: str
    revision: int
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    my_role: str | None = None


class MemberAdd(_Strict):
    principal_type: Literal["user", "admin"]
    username: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=32)


class MemberOut(BaseModel):
    principal_type: str
    principal_id: int
    username: str
    role: str


class BoundaryCreate(_Strict):
    geojson: dict
    reason: str = Field(min_length=1)


class BoundaryOut(BaseModel):
    id: str
    version: int
    crs: str
    area_ha: float
    area_method: str
    checksum: str
    created_by: str
    reason: str
    created_at: dt.datetime | None
    validated_geojson: dict | None = None
    # Present on creation only: count and area of overlap with other projects.
    # Never carries ids or names of those projects.
    overlap: dict | None = None
