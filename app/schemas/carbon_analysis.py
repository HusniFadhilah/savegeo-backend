"""Bounded common input fields for synchronous carbon analysis requests."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CarbonAnalysisRequest(BaseModel):
    # The legacy route accepts several model-specific optional settings. Keep
    # those extensible while validating shared high-risk inputs and applying
    # the byte/depth/value bounds in ``read_typed_json_object``.
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    aoi: dict[str, Any] | None = None
    model_name: str | None = Field(default=None, max_length=128)
    calibration_model_id: int | None = Field(default=None, ge=1)
    reference_dataset: str | None = Field(default=None, max_length=128)
    dataset_year: int | None = Field(default=None, ge=1980, le=2100)
    year: int | None = Field(default=None, ge=1980, le=2100)
    start_month: int | None = Field(default=None, ge=1, le=12)
    end_month: int | None = Field(default=None, ge=1, le=12)
    start_date: str | None = Field(default=None, max_length=20)
    end_date: str | None = Field(default=None, max_length=20)
    scale: float | None = Field(default=None, gt=0, le=100_000)
    cloud_threshold: float | None = Field(default=None, ge=0, le=100)
    vis_min: float | None = None
    vis_max: float | None = None
    vis_palette: list[str] | None = Field(default=None, max_length=12)
    simple_mode: bool | None = None
    clip_to_aoi: bool | None = None
    reference_only: bool | None = None
    load_only: bool | None = None
