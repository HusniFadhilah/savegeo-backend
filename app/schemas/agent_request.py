"""Typed, bounded request fields used by the SaveGeo assistant endpoints."""
import base64
import binascii
import struct
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_AGENT_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_AGENT_IMAGE_BASE64_CHARS = 5_592_408
_MAX_AGENT_IMAGE_PIXELS = 2_000_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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
    image: str | None = Field(default=None, max_length=_MAX_AGENT_IMAGE_BASE64_CHARS)
    attachment: dict[str, Any] | None = None

    @field_validator("image")
    @classmethod
    def validate_screenshot(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            image = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Screenshot must be valid base64-encoded PNG data") from exc
        if len(image) > _MAX_AGENT_IMAGE_BYTES or len(image) < 24 or not image.startswith(_PNG_SIGNATURE):
            raise ValueError("Screenshot must be a PNG no larger than 4 MiB")
        if image[12:16] != b"IHDR":
            raise ValueError("Screenshot PNG header is invalid")
        width, height = struct.unpack(">II", image[16:24])
        if width < 1 or height < 1 or width * height > _MAX_AGENT_IMAGE_PIXELS:
            raise ValueError("Screenshot dimensions exceed the 2 megapixel limit")
        return value
