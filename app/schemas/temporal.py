"""Pydantic temporal contracts shared by API routes."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.temporal import format_rfc3339, parse_rfc3339


class TemporalInterval(BaseModel):
    start: dt.datetime
    end: dt.datetime
    semantics: str = Field(default="[start,end)", pattern=r"^\[start,end\)$")

    @field_validator("start", "end", mode="before")
    @classmethod
    def parse_absolute(cls, value: object, info):
        return parse_rfc3339(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_order(self) -> "TemporalInterval":
        if self.start >= self.end:
            raise ValueError("temporal interval must satisfy start < end")
        return self

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        data["start"] = format_rfc3339(self.start, field="start")
        data["end"] = format_rfc3339(self.end, field="end")
        return data
