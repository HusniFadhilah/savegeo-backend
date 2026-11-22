"""Typed, bounded request fields used by the SaveGeo assistant endpoints."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentRequest(BaseModel):
    # Agent capabilities grow independently; explicit high-risk values are typed,
    # while the bounded context extensions remain available to existing tools.
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    message: str = Field(default="", max_length=20_000)
    session_id: int | None = Field(default=None, gt=0)
    mode: Literal["geoai", "control"] | None = None
    execute: bool | None = None
    page_state: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    image: str | None = Field(default=None, max_length=9_000_000)
    attachment: dict[str, Any] | None = None
