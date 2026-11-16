from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalysisParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str | None = Field(default=None, max_length=128)
    reference_dataset: str = Field(default="WCMC", min_length=1, max_length=128)
    datasets: list[str] = Field(default_factory=lambda: ["Dynamic_World"], min_length=1, max_length=12)
    dataset: str = Field(default="Dynamic_World", min_length=1, max_length=128)
    indices: list[str] = Field(default_factory=lambda: ["NDVI"], min_length=1, max_length=8)
    satellite: str = Field(default="sentinel2", min_length=1, max_length=64)
    cloud_threshold: int = Field(default=20, ge=0, le=100)
    scale: int = Field(default=30, ge=10, le=1000)
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    reference_only: bool = False


class ProjectAnalysisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["carbon", "landcover", "landcover_transition", "vegetation"]
    monitoring_period_id: str = Field(min_length=1, max_length=36)
    baseline_period_id: str | None = Field(default=None, min_length=1, max_length=36)
    parameters: AnalysisParameters = Field(default_factory=AnalysisParameters)
