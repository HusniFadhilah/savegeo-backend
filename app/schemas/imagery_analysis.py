"""Typed, bounded request envelope for imagery analysis endpoints."""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ImageryAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    aoi: dict[str, Any] | None = None
    geometry: dict[str, Any] | None = None
    satellite: str | None = Field(default=None, max_length=80)
    provider_key: str | None = Field(default=None, max_length=80)
    scene_id: str | None = Field(default=None, max_length=180)
    item_url: str | None = Field(default=None, max_length=4096)
    asset_key: str | None = Field(default=None, max_length=80)
    cog_asset_key: str | None = Field(default=None, max_length=80)
    start_date: str | None = Field(default=None, max_length=40)
    end_date: str | None = Field(default=None, max_length=40)
    max_cloud_cover: float | None = Field(default=None, ge=0, le=100)
    limit: int | None = Field(default=None, ge=1, le=2000)
    max_items: int | None = Field(default=None, ge=1, le=2000)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    wavelengths_nm: list[float] | None = Field(default=None, max_length=256)
    values: list[float] | None = Field(default=None, max_length=100_000)
    scale: float | None = Field(default=None, gt=0, le=1_000_000)
    offset: float | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    source_unit: str | None = Field(default=None, max_length=16)
    output_unit: str | None = Field(default=None, max_length=16)
    bands: str | list[int] | None = Field(default=None, max_length=256)
    rescale: str | None = Field(default=None, max_length=256)
    master: dict[str, Any] | None = None
    slave: dict[str, Any] | None = None

    @field_validator("item_url")
    @classmethod
    def item_url_is_https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError("item_url harus berupa URL HTTPS tanpa kredensial.")
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def date_is_iso(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from datetime import date, datetime

        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("Tanggal harus memakai format ISO-8601.") from exc
        return value

    @field_validator("wavelengths_nm", "values")
    @classmethod
    def finite_series(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and any(not math.isfinite(number) for number in value):
            raise ValueError("Semua nilai numerik harus finite.")
        return value

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "ImageryAnalysisRequest":
        if self.start_date and self.end_date and self.start_date[:10] > self.end_date[:10]:
            raise ValueError("start_date harus sama atau lebih awal daripada end_date.")
        return self
