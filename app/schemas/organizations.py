"""Strict workspace contracts; role changes cannot change tenant or identity."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OrganizationCreate(Strict):
    name: str = Field(min_length=1, max_length=255)


class OrganizationOut(BaseModel):
    id: str
    name: str
    role: Literal["owner", "admin", "member"]
    revision: int
    personal: bool


class MemberChange(Strict):
    role: Literal["owner", "admin", "member"]
    is_active: bool = True


class InvitationCreate(Strict):
    email: str = Field(min_length=3, max_length=128)
    role: Literal["admin", "member"] = "member"
    expires_in_hours: int = Field(default=72, ge=1, le=168)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value: str) -> str:
        value = value.casefold()
        if value.count("@") != 1 or any(c.isspace() for c in value) or not all(value.split("@")):
            raise ValueError("Alamat email tidak valid.")
        return value


class InvitationAccept(Strict):
    token: str = Field(min_length=32, max_length=256)


class InvitationOut(BaseModel):
    id: str
    email: str
    role: str
    expires_at: dt.datetime
    accepted_at: dt.datetime | None
    revoked_at: dt.datetime | None
