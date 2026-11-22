"""Typed input contracts for bounded disaster-analysis requests."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _DisasterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AoiRequest(_DisasterRequest):
    aoi: dict[str, Any]
    scale: int | None = Field(default=None, ge=1, le=100_000)


class EventMapRequest(AoiRequest):
    event_type: Literal["fire", "flood", "landslide"] = "flood"
    before_start: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    before_end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    after_start: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    after_end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    dnbr_threshold: float | None = Field(default=None, ge=-1, le=2)
    water_threshold: float | None = Field(default=None, ge=-100, le=10)
    change_threshold: float | None = Field(default=None, ge=-10, le=10)
    ndvi_drop_threshold: float | None = Field(default=None, ge=0, le=2)
    bsi_threshold: float | None = Field(default=None, ge=-2, le=2)


class FireMultiSourceRequest(_DisasterRequest):
    aoi: dict[str, Any]
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sources: list[str] | None = Field(default=None, max_length=9)


class FireBoundariesRequest(_DisasterRequest):
    province: str = Field(min_length=1, max_length=100)
    city: str | None = Field(default=None, max_length=200)


class FireImportRequest(_DisasterRequest):
    content: str = Field(min_length=1, max_length=2_000_000)
    format: Literal["csv", "geojson"]
    source: str | None = Field(default=None, max_length=100)


class FireSamRequest(AoiRequest):
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    max_cloud_cover: int | None = Field(default=None, ge=0, le=100)
    seed_radius_px: int | None = Field(default=None, ge=1, le=64)
