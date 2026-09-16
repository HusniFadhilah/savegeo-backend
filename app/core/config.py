"""Application settings, sourced from environment variables / .env.

Mirrors the legacy Flask `app.config` defaults (see backend/app.py lines ~40-90)
but centralizes them via pydantic-settings instead of scattering `os.environ.get`
calls through route handlers.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "development"
    debug: bool = True
    api_prefix: str = "/api"
    port: int = 8086
    app_name: str = "SAVEGEO"
    frontend_base_url: str = "http://localhost:5501"

    # --- Database ---
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"

    # --- Supabase ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_gee_credentials_bucket: str = "gee-credentials"

    # --- JWT ---
    # Production must provide a stable secret through the environment/secret
    # manager. Development can still receive an ephemeral secret so a local
    # checkout works without creating a credential file.
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440
    password_reset_token_expire_minutes: int = 30
    # Browser sessions use HttpOnly cookies. Secure is forced for production
    # below, while local HTTP development remains usable with Secure=false.
    auth_cookie_secure: bool | None = None
    auth_cookie_samesite: str = "lax"
    csrf_protection_enabled: bool = True
    rate_limit_requests_per_minute: int = 120

    # --- Mail / SMTP ---
    mail_mailer: str = "smtp"
    mail_host: str = ""
    mail_port: int = 587
    mail_username: str = ""
    mail_password: str = ""
    mail_encryption: str = "tls"
    mail_from_address: str = ""
    mail_from_name: str = ""

    # --- CORS ---
    allowed_origins: str = "http://localhost:5500,http://localhost:3000,http://localhost:5501,https://savegeo.len.co.id,https://begeo.len.co.id"

    # --- GEE fallback (used only if no active DB credential row exists) ---
    gee_service_account: str = ""
    gee_key_file: str = ""
    gee_project_id: str = ""

    # --- Local storage ---
    upload_dir: str = "./var/uploads"
    model_dir: str = "./var/saved_models"
    # Self-hosted disaster-imagery rasters (e.g. commercial GeoTIFFs not in the
    # public GEE catalog) served as XYZ tiles by app/services/local_tile_service.py -
    # chosen specifically to avoid needing a billed GCS bucket for GEE asset
    # ingestion (see savegeo/backend/docs/ntt-earthquake-integration-prompt.md).
    disaster_raster_dir: str = "./var/disaster_rasters"

    # --- Copernicus Data Space Ecosystem (GeoSave Engine STAC ingestion) ---
    # Public STAC metadata can be searched anonymously, but pixel assets expose
    # authenticated S3 / OIDC HTTPS hrefs. Put a short-lived CDSE bearer token
    # here, or configure AWS_* credentials in the process environment for S3.
    copernicus_cdse_access_token: str = ""

    # --- Licensed commercial EO providers (server-side only) ---
    # These are intentionally backend-only.  The STAC URL may point to the
    # provider catalog or its /search endpoint; credentials never enter the
    # browser bundle or the imagery scene payload.
    planet_stac_url: str = ""
    planet_stac_collection: str = ""
    planet_api_key: str = ""
    planet_auth_scheme: str = "basic"
    vantor_stac_url: str = ""
    vantor_stac_collection: str = ""
    vantor_api_token: str = ""
    vantor_auth_scheme: str = "bearer"
    iceye_stac_url: str = ""
    iceye_stac_collection: str = ""
    iceye_api_token: str = ""
    iceye_auth_scheme: str = "bearer"
    commercial_imagery_timeout_seconds: int = 30

    # --- Chloris carbon stock integration ---
    # Do not store Chloris username/password here. Use API credentials from the
    # Chloris profile page, or configure a direct dataPath/download URL.
    chloris_base_url: str = "https://app.chloris.earth"
    chloris_organization_id: str = ""
    chloris_reporting_unit_id: str = ""
    chloris_id_token: str = ""
    chloris_refresh_token: str = ""
    chloris_data_path: str = ""

    # --- Region API ---
    region_api_base_url: str = "https://api.sp3stab.id/api/en"

    # --- Disaster adapters ---
    bmkg_cap_url: str = "https://www.bmkg.go.id/alerts/nowcast/id"
    nasa_firms_map_key: str = ""
    nasa_firms_base_url: str = "https://firms.modaps.eosdis.nasa.gov"
    nasa_firms_cache_ttl_seconds: int = 900
    nasa_firms_request_timeout_seconds: int = 20
    nasa_firms_max_day_range: int = 7
    inarisk_wms_url: str = ""
    inarisk_wms_layers: str = ""
    inarisk_tile_url: str = ""
    demnas_wms_url: str = ""
    demnas_wms_layers: str = ""
    demnas_tile_url: str = ""
    demnas_ee_asset: str = ""

    # --- ArcGIS ---
    arcgis_enabled: bool = True
    arcgis_portal_url: str = "https://www.arcgis.com"
    arcgis_auth_mode: str = "none"
    arcgis_api_key: str = ""
    arcgis_request_timeout: int = 30

    # --- Agentic AI ---
    ai_provider: str = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""
    deepseek_api_key: str = ""
    gemini_api_key: str = ""

    # --- Analysis defaults (ported from legacy app.config, overridable via system_config table) ---
    default_cloud_threshold: int = 40
    default_carbon_scale: int = 10
    default_vegetation_scale: int = 10
    default_landcover_scale: int = 30
    max_pixels: int = 1_000_000_000
    carbon_co2_factor: float = 3.6667  # 44/12
    carbon_vis_min: float = 0
    carbon_vis_max: float = 200
    carbon_vis_palette: str = "440154,414487,2a788e,22a884,7ad151,fde725"
    carbon_legend_bins: int = 6

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"dev", "development"}:
                return True
        return value

    @model_validator(mode="after")
    def require_production_secrets(self) -> Settings:
        if self.app_env.lower() in {"prod", "production"} and len(self.jwt_secret_key) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters in production")
        if not self.jwt_secret_key:
            self.jwt_secret_key = secrets.token_urlsafe(48)
        if self.auth_cookie_samesite.lower() not in {"lax", "strict", "none"}:
            raise ValueError("AUTH_COOKIE_SAMESITE must be lax, strict, or none")
        return self

    @property
    def session_cookie_secure(self) -> bool:
        """Use Secure cookies automatically outside local development."""
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.app_env.lower() in {"prod", "production", "staging"}

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def model_path(self) -> Path:
        p = Path(self.model_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def disaster_raster_path(self) -> Path:
        p = Path(self.disaster_raster_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
