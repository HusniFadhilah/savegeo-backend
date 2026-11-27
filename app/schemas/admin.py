from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator
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
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    key: str = Field(min_length=1, max_length=200)
    value: Any


class ConfigBulkUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)  # legacy bare {key: value, ...} remains supported

    updates: list[ConfigUpdateItem] | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_config_update_shape(cls, value):
        if not isinstance(value, dict) or len(value) > 500:
            raise ValueError("Konfigurasi harus berupa objek dengan paling banyak 500 kunci.")
        if "updates" in value:
            if set(value) != {"updates"} or not isinstance(value["updates"], list) or len(value["updates"]) > 500:
                raise ValueError("updates harus berupa daftar maksimal 500 item key/value.")
        elif any(not isinstance(key, str) or not key or len(key) > 200 for key in value):
            raise ValueError("Nama kunci konfigurasi harus berupa teks 1–200 karakter.")
        return value


class ArcGISConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool
    portal_url: str = Field(min_length=1, max_length=2048)
    auth_mode: str = Field(pattern="^(none|api_key)$")
    request_timeout: int = Field(default=30, ge=1, le=120)
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("portal_url")
    @classmethod
    def portal_must_be_an_allowed_https_host(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        from app.providers.arcgis_client import _is_allowed_domain

        if (
            parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.query or parsed.fragment
            or not _is_allowed_domain(value.strip())
        ):
            raise ValueError("portal_url harus HTTPS dan memakai domain ArcGIS yang diizinkan.")
        return value.strip().rstrip("/")


class CompanyBoundaryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=255)
    industry_type: str = Field(min_length=1, max_length=100)
    company_name: str | None = Field(default=None, max_length=255)
    sub_type: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=160)
    district: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=5000)
    geojson: dict[str, Any] | None = None
    geojson_text: str | None = Field(default=None, max_length=10_000_000)


class CompanyBoundaryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    industry_type: str | None = Field(default=None, min_length=1, max_length=100)
    sub_type: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=160)
    district: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=5000)
    is_active: bool | None = None
    geojson: dict[str, Any] | str | None = None

    @field_validator("geojson", mode="before")
    @classmethod
    def parse_and_check_geojson(cls, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError) as exc:
                raise ValueError("geojson harus berisi objek GeoJSON yang valid.") from exc
        if value is not None and not isinstance(value, dict):
            raise ValueError("geojson harus berupa objek GeoJSON.")
        if isinstance(value, dict) and value.get("type") not in {
            "Feature", "FeatureCollection", "Polygon", "MultiPolygon", "GeometryCollection",
        }:
            raise ValueError("Tipe GeoJSON tidak didukung untuk batas perusahaan.")
        return value

    @model_validator(mode="after")
    def reject_null_active_or_geometry(self):
        if not self.model_fields_set:
            raise ValueError("Minimal satu field batas perusahaan harus dikirim.")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name tidak boleh bernilai null.")
        if "industry_type" in self.model_fields_set and self.industry_type is None:
            raise ValueError("industry_type tidak boleh bernilai null.")
        if "is_active" in self.model_fields_set and self.is_active is None:
            raise ValueError("is_active harus berupa boolean.")
        if "geojson" in self.model_fields_set and self.geojson is None:
            raise ValueError("geojson tidak boleh bernilai null.")
        return self


class CompanyOSMImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    industry_types: list[StrictStr] | None = Field(default=None, max_length=20)


class ImageryProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    group: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=10000)
    resolution_m: float | None = Field(default=None, ge=0, le=100000)
    revisit_days: float | None = Field(default=None, ge=0, le=3650)
    start_year: int | None = Field(default=None, ge=1900, le=2200)
    gee_collection: str | None = Field(default=None, max_length=2048)
    cloud_property: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Field yang dikirim tidak boleh bernilai null.")
        if not self.model_fields_set:
            raise ValueError("Minimal satu field provider harus dikirim.")
        return self


class ImageryProviderConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    fields: dict[str, StrictStr] = Field(default_factory=dict, max_length=10)
    credential: str | None = Field(default=None, max_length=4096)
    clear_credential: bool | None = None

    @model_validator(mode="after")
    def reject_conflicting_secret_actions(self):
        if self.credential and self.clear_credential:
            raise ValueError("Credential baru dan penghapusan credential tidak dapat dikirim bersamaan.")
        return self


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
