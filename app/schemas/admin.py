from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class PasswordForgotRequest(BaseModel):
    identifier: str


class PasswordResetRequest(BaseModel):
    token: str
    password: str


class ConfigUpdateItem(BaseModel):
    key: str
    value: Any


class ConfigBulkUpdateRequest(BaseModel):
    updates: list[ConfigUpdateItem] | None = None

    class Config:
        extra = "allow"  # legacy also accepts a bare {key: value, ...} object


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
    display_name: str | None = None
    algorithm: str | None = None
    description: str | None = None
    version: str | None = None
    is_active: bool | None = None
    metrics: dict | None = None
    feature_names: list | None = None
    metadata_json: dict | None = None


class AdminUserCreateRequest(BaseModel):
    username: str
    password: str
    email: str | None = None
    role_id: int | None = None
    is_active: bool = True


class AdminUserUpdateRequest(BaseModel):
    email: str | None = None
    is_active: bool | None = None
    role_id: int | None = None
    # Admin-initiated password reset for another account (distinct from the
    # self-service "old_password + new_password" change-password flow).
    new_password: str | None = None
