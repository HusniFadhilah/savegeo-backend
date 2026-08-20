from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ConfigUpdateItem(BaseModel):
    key: str
    value: Any


class ConfigBulkUpdateRequest(BaseModel):
    updates: Optional[list[ConfigUpdateItem]] = None

    class Config:
        extra = "allow"  # legacy also accepts a bare {key: value, ...} object


class ConfigResetRequest(BaseModel):
    key: Optional[str] = None


class SatelliteProviderUpdateRequest(BaseModel):
    """Every field optional - PUT is a partial upsert onto the satellite_providers
    overlay row (app/db/models/satellite_provider_entry.py). A null/omitted
    field leaves that field falling back to the static registry default."""
    name: Optional[str] = None
    provider: Optional[str] = None
    resolution_label: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    display_order: Optional[int] = None
    gee_collection: Optional[str] = None
    band_role_map: Optional[dict] = None
    resolution_m: Optional[int] = None
    revisit_days: Optional[int] = None
    swath_km: Optional[int] = None
    launch: Optional[str] = None
    start_year: Optional[int] = None
    bands_available: Optional[list] = None


class ModelUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    algorithm: Optional[str] = None
    description: Optional[str] = None
    version: Optional[str] = None
    is_active: Optional[bool] = None
    metrics: Optional[dict] = None
    feature_names: Optional[list] = None
    metadata_json: Optional[dict] = None


class AdminUserCreateRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    role_id: Optional[int] = None
    is_active: bool = True


class AdminUserUpdateRequest(BaseModel):
    email: Optional[str] = None
    is_active: Optional[bool] = None
    role_id: Optional[int] = None
    # Admin-initiated password reset for another account (distinct from the
    # self-service "old_password + new_password" change-password flow).
    new_password: Optional[str] = None
