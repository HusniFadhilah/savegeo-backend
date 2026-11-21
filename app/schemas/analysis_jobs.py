"""Typed request contract for long-running analysis jobs.

The payload remains extensible because analysis modules have different
parameters, but shared inputs are typed and common resource-sensitive values
are validated before any provider or worker is invoked.
"""
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_YEAR_FIELDS = frozenset({
    "year", "analysis_year", "analysisYear", "start_year", "end_year",
    "from_year", "to_year", "year_from", "year_to", "reference_year",
    "dataset_year", "transition_start_year", "transition_end_year",
})
_CLOUD_FIELDS = frozenset({"cloud", "cloud_cover", "cloudCover", "cloud_threshold", "max_cloud_pct"})
_SCALE_FIELDS = frozenset({"scale", "scale_m", "resolution", "resolution_m", "native_scale"})
_COUNT_FIELDS = frozenset({"limit", "page_size", "top_n", "max_features", "sample_count"})


def _validate_payload_limits(value: Any) -> None:
    """Check shared bounds without rejecting model-specific extension fields."""
    stack = [value]
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if visited > 100_000:
            raise ValueError("Payload exceeds the supported number of values.")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256 or key in {"__proto__", "constructor", "prototype"}:
                    raise ValueError("Payload contains an unsupported key.")
                if isinstance(child, bool) or child is None:
                    stack.append(child)
                    continue
                if key in _YEAR_FIELDS:
                    try:
                        year_value = int(child)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(f"{key} must be a year from 1980 to 2200.") from exc
                    if str(year_value) != str(child) or not 1980 <= year_value <= 2200:
                        raise ValueError(f"{key} must be a year from 1980 to 2200.")
                elif key in _CLOUD_FIELDS:
                    try:
                        cloud_value = float(child)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(f"{key} must be between 0 and 100.") from exc
                    if not math.isfinite(cloud_value) or not 0 <= cloud_value <= 100:
                        raise ValueError(f"{key} must be between 0 and 100.")
                elif key in _SCALE_FIELDS:
                    try:
                        scale_value = float(child)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(f"{key} must be between 0.25 and 100000 meters.") from exc
                    if not math.isfinite(scale_value) or not 0.25 <= scale_value <= 100_000:
                        raise ValueError(f"{key} must be between 0.25 and 100000 meters.")
                elif key in _COUNT_FIELDS:
                    if type(child) is not int or child < 0 or child > 250_000:
                        raise ValueError(f"{key} must be an integer from 0 to 250000.")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)


class AnalysisPayload(BaseModel):
    """Shared typed envelope for carbon, vegetation, LULC, and crop jobs."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    aoi: Any | None = None
    geometry: Any | None = None
    geojson: Any | None = None
    year: int | None = Field(default=None, ge=1980, le=2200)
    analysis_year: int | None = Field(default=None, ge=1980, le=2200)
    start_month: int | None = Field(default=None, ge=1, le=12)
    end_month: int | None = Field(default=None, ge=1, le=12)
    scale: float | None = Field(default=None, gt=0.0, le=100_000)
    cloud_threshold: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def payload_is_bounded_object(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value:
            raise ValueError("payload must be a non-empty JSON object.")
        _validate_payload_limits(value)
        return value


class AnalysisJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    type: Literal[
        "carbon", "carbon_local", "carbon_delta", "geotiff", "vegetation", "landcover",
        "landcover_change", "landcover_transition", "landcover_change_map", "landcover_hotspots",
        "landcover_fusion", "crop_monitoring",
    ]
    payload: AnalysisPayload
