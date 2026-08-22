"""Application settings, sourced from environment variables / .env.

Mirrors the legacy Flask `app.config` defaults (see backend/app.py lines ~40-90)
but centralizes them via pydantic-settings instead of scattering `os.environ.get`
calls through route handlers.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "development"
    debug: bool = True
    api_prefix: str = "/api"
    port: int = 8086

    # --- Database ---
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"

    # --- Supabase ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_gee_credentials_bucket: str = "gee-credentials"

    # --- JWT ---
    jwt_secret_key: str = "insecure-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440

    # --- CORS ---
    allowed_origins: str = "http://localhost:5500,http://localhost:3000,http://localhost:5501,https://savegeo.len.co.id"

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

    # --- Region API ---
    region_api_base_url: str = "https://api.sp3stab.id/api/en"

    # --- Disaster adapters ---
    bmkg_cap_url: str = "https://www.bmkg.go.id/alerts/nowcast/id"
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
    default_carbon_scale: int = 100
    default_vegetation_scale: int = 10
    default_landcover_scale: int = 30
    max_pixels: int = 1_000_000_000
    carbon_co2_factor: float = 3.6667  # 44/12
    carbon_vis_min: float = 0
    carbon_vis_max: float = 250
    carbon_vis_palette: str = "d73027,fee08b,1a9850"
    carbon_legend_bins: int = 6

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
