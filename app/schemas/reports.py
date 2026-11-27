"""Request contracts for reports generated from client-visible analyses."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExecutiveSummaryRequest(BaseModel):
    """Bounded, typed envelope for the legacy client-built report endpoint.

    Report sections remain extensible while known top-level values are typed.
    The authoritative saved-job report endpoint does not accept client metrics.
    """

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    title: str | None = Field(default=None, max_length=255)
    aoi_name: str | None = Field(default=None, max_length=160)
    generated_by: str | None = Field(default=None, max_length=160)
    period: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    results: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    limitations: list[str] | None = Field(default=None, max_length=100)
