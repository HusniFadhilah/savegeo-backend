"""Typed request contract for long-running analysis jobs."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalysisJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    type: Literal[
        "carbon", "carbon_local", "carbon_delta", "geotiff", "vegetation", "landcover",
        "landcover_change", "landcover_transition", "landcover_change_map", "landcover_hotspots",
        "landcover_fusion", "crop_monitoring",
    ]
    payload: dict[str, Any] = Field(min_length=1, max_length=10_000)
