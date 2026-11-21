from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.schemas.profile import FullNameFields


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class AdminProfileUpdateRequest(BaseModel):
    username: str
    email: str | None = None
    full_name: str = Field(max_length=160)

    @field_validator("full_name")
    @classmethod
    def require_full_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Nama lengkap wajib diisi")
        return value


class AdminNameUpdateRequest(BaseModel):
    full_name: str = Field(max_length=160)

    @field_validator("full_name")
    @classmethod
    def require_full_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Nama lengkap wajib diisi")
        return value


class PasswordForgotRequest(BaseModel):
    identifier: str


class PasswordResetRequest(BaseModel):
    token: str
    password: str


class ConfigUpdateItem(BaseModel):
    key: str
    value: Any


class ConfigBulkUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # legacy also accepts a bare {key: value, ...} object

    updates: list[ConfigUpdateItem] | None = None


class ConfigResetRequest(BaseModel):
    key: str | None = None


class SatelliteProviderUpdateRequest(BaseModel):
    """Every field optional - PUT is a partial upsert onto the satellite_providers
    overlay row (app/db/models/satellite_provider_entry.py). A null/omitted
    field leaves that field falling back to the static registry default."""
    name: str | None = None
    provider: str | None = None
    resolution_label: str | None = None
    description: str | None = None
    is_active: bool | None = None
    display_order: int | None = None
    gee_collection: str | None = None
    band_role_map: dict | None = None
    resolution_m: int | None = None
    revisit_days: int | None = None
    swath_km: int | None = None
    launch: str | None = None
    start_year: int | None = None
    bands_available: list | None = None


class ModelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)

    @field_validator("display_name")
    @classmethod
    def trim_model_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("display_name cannot be blank")
        return value


class ModelReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    reason: str = Field(min_length=10, max_length=2000)


class AdminUserCreateRequest(BaseModel):
    is_superuser: bool = False
    full_name: str = Field(max_length=160)
    username: str
    password: str
    email: str | None = None
    role_id: int | None = None
    is_active: bool = True
    mfa_required: bool = False

    @field_validator("full_name")
    @classmethod
    def require_full_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Nama lengkap wajib diisi")
        return value


class AdminUserUpdateRequest(FullNameFields):
    is_superuser: bool | None = None
    email: str | None = None
    is_active: bool | None = None
    mfa_required: bool | None = None
    role_id: int | None = None
    # Admin-initiated password reset for another account (distinct from the
    # self-service "old_password + new_password" change-password flow).
    new_password: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_blank_full_name(cls, value: Any) -> Any:
        if isinstance(value, dict) and "full_name" in value:
            full_name = value["full_name"]
            if not isinstance(full_name, str) or not full_name.strip():
                raise ValueError("Nama lengkap wajib diisi")
            value = {**value, "full_name": full_name.strip()}
        return value
