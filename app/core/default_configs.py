"""Default `system_config` seed rows, ported verbatim from legacy `backend/database.py`
(`DEFAULT_CONFIGS`). Tuple shape: (key, value, value_type, category, label, description, is_public).
"""
from __future__ import annotations

import datetime as dt
import os

DEFAULT_CONFIGS: list[tuple[str, str, str, str, str, str, bool]] = [
    ("analysis.cloud_threshold", os.getenv("DEFAULT_CLOUD_THRESHOLD", "10"), "int", "analysis",
     "Cloud threshold (%)", "Max cloud cover for Sentinel-2 filtering", False),

    ("analysis.carbon_scale", os.getenv("DEFAULT_CARBON_SCALE", "250"), "int", "analysis",
     "Carbon analysis scale (m)", "Spatial resolution for carbon estimation", False),

    ("analysis.veg_scale", os.getenv("DEFAULT_VEG_SCALE", "10"), "int", "analysis",
     "Vegetation analysis scale (m)", "Spatial resolution for vegetation indices", False),

    ("analysis.lc_scale", os.getenv("DEFAULT_LC_SCALE", "10"), "int", "analysis",
     "Land cover scale (m)", "Spatial resolution for land cover", False),

    ("analysis.num_pixels", os.getenv("DEFAULT_NUM_PIXELS", "5000"), "int", "analysis",
     "Sample pixels", "Number of pixels for sampling", False),

    ("analysis.max_pixels", os.getenv("DEFAULT_MAX_PIXELS", "1e13"), "float", "analysis",
     "Max pixels", "Maximum pixels for reduceRegion", False),

    ("year.min", os.getenv("DEFAULT_YEAR_MIN", "2015"), "int", "year",
     "Min year", "Oldest year available", True),

    ("year.max", os.getenv("DEFAULT_YEAR_MAX", str(dt.datetime.now(dt.UTC).year)), "int", "year",
     "Max year", "Latest year available", True),

    ("year.esri_min", os.getenv("ESRI_YEAR_MIN", "2017"), "int", "year",
     "ESRI min year", "First ESRI LC year", True),

    ("year.esri_max", os.getenv("ESRI_YEAR_MAX", "2023"), "int", "year",
     "ESRI max year", "Last ESRI LC year", True),

    ("year.esa_threshold", os.getenv("ESA_THRESHOLD_YEAR", "2021"), "int", "year",
     "ESA threshold year", "Year for ESA v200 vs v100", True),

    ("carbon.vis_min", os.getenv("CARBON_VIS_MIN", "0"), "int", "carbon",
     "Carbon vis min (Mg/ha)", "Min for carbon colormap", False),

    ("carbon.vis_max", os.getenv("CARBON_VIS_MAX", "200"), "int", "carbon",
     "Carbon vis max (Mg/ha)", "Max for carbon colormap", False),

    ("carbon.vis_palette", os.getenv("CARBON_VIS_PALETTE", "440154,414487,2a788e,22a884,7ad151,fde725"), "string", "carbon",
     "Carbon palette", "Comma-separated hex colors", False),

    ("carbon.legend_bins", os.getenv("CARBON_LEGEND_BINS", "6"), "int", "carbon",
     "Carbon legend ranges", "Number of ranges shown in the carbon density legend", True),

    ("carbon.co2_factor", os.getenv("CO2_CONVERSION_FACTOR", "3.67"), "float", "carbon",
     "CO₂ conversion factor", "IPCC C→CO₂ factor", True),

    ("app.allowed_origins", os.getenv("ALLOWED_ORIGINS", "http://localhost:5500,http://localhost:3000"), "string", "app",
     "Allowed CORS origins", "Comma-separated origins", False),

    ("app.region_api_base", os.getenv("REGION_API_BASE_URL", "https://api.sp3stab.id/api/en"), "string", "app",
     "Region API base URL", "External region data API", False),

    ("app.esri_collection_id", os.getenv("ESRI_COLLECTION_ID", "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS"), "string", "app",
     "ESRI collection ID", "GEE asset ID", False),

    ("app.name", os.getenv("APP_NAME", "GeoMoka Platform"), "string", "app",
     "Platform name", "Shown in UI", True),

    ("app.version", os.getenv("APP_VERSION", "1.0.0"), "string", "app",
     "Platform version", "", True),

    # -- AI controller --
    ("ai.provider", os.getenv("AI_PROVIDER", "anthropic"), "string", "ai",
     "AI Provider", "Provider aktif: anthropic | openai | openrouter | deepseek | gemini", False),

    ("ai.model", os.getenv("AI_MODEL", ""), "string", "ai",
     "Model", "Nama model (kosong = default provider)", False),

    ("ai.anthropic_api_key", os.getenv("ANTHROPIC_API_KEY", ""), "string", "ai",
     "Anthropic API Key", "sk-ant-... dari console.anthropic.com", False),

    ("ai.openai_api_key", os.getenv("OPENAI_API_KEY", ""), "string", "ai",
     "OpenAI API Key", "sk-... dari platform.openai.com", False),

    ("ai.openrouter_api_key", os.getenv("OPENROUTER_API_KEY", ""), "string", "ai",
     "OpenRouter API Key", "sk-or-... dari openrouter.ai", False),

    ("ai.deepseek_api_key", os.getenv("DEEPSEEK_API_KEY", ""), "string", "ai",
     "DeepSeek API Key", "sk-... dari platform.deepseek.com", False),

    ("ai.gemini_api_key", os.getenv("GEMINI_API_KEY", ""), "string", "ai",
     "Gemini API Key", "AIza... dari aistudio.google.com", False),

    ("ai.custom_base_url", os.getenv("AI_CUSTOM_BASE_URL", ""), "string", "ai",
     "Custom Base URL", "Override base URL untuk provider openai-compatible lainnya", False),

    # -- Rate limiting --
    ("ai.rate_limit_rpm", os.getenv("AI_RATE_LIMIT_RPM", "10"), "int", "ai",
     "Rate Limit (req/menit)", "Maks permintaan AI per menit per pengguna (0 = tidak dibatasi)", True),

    ("ai.rate_limit_daily", os.getenv("AI_RATE_LIMIT_DAILY", "200"), "int", "ai",
     "Rate Limit (req/hari)", "Maks permintaan AI per hari per pengguna (0 = tidak dibatasi)", True),

    ("ai.rate_limit_max_chars", os.getenv("AI_RATE_LIMIT_MAX_CHARS", "4000"), "int", "ai",
     "Maks Karakter Pesan", "Panjang maksimum pesan + konteks yang dikirim ke AI", True),

    ("ai.rate_limit_max_file_mb", os.getenv("AI_RATE_LIMIT_MAX_FILE_MB", "5"), "int", "ai",
     "Maks Ukuran File (MB)", "Ukuran maksimum file/foto yang bisa diupload ke chatbot", True),

    # -- Crop Monitoring risk score weights (must sum to 1.0; admin-editable,
    # never hardcoded in crop_monitoring_service - see _risk_score()) --
    ("crop_risk.weight_vegetation", "0.30", "float", "crop_monitoring",
     "Bobot: Anomali Vegetasi", "Kontribusi anomali NDVI terhadap Crop Risk Score", True),

    ("crop_risk.weight_moisture", "0.20", "float", "crop_monitoring",
     "Bobot: Kelembapan", "Kontribusi defisit curah hujan terhadap Crop Risk Score", True),

    ("crop_risk.weight_weather", "0.20", "float", "crop_monitoring",
     "Bobot: Cuaca", "Kontribusi peringatan cuaca (hari kering/heat stress) terhadap Crop Risk Score", True),

    ("crop_risk.weight_flood", "0.15", "float", "crop_monitoring",
     "Bobot: Banjir", "Kontribusi luas area tergenang terhadap Crop Risk Score", True),

    ("crop_risk.weight_growth_anomaly", "0.15", "float", "crop_monitoring",
     "Bobot: Anomali Fase Pertumbuhan", "Kontribusi anomali fase pertumbuhan terhadap Crop Risk Score (belum aktif di V1)", True),
]

# Keys with these suffixes are treated as secrets: masked on GET, write-only on PUT
# (empty-value PUT is a no-op so a masked GET->PUT roundtrip can't accidentally wipe them).
SECRET_KEY_SUFFIXES = ("_api_key", "_secret", "_password", "_token", "_private_key", "_secret_key", "_api_secret")


def is_secret_key(key: str) -> bool:
    return any(key.endswith(suffix) for suffix in SECRET_KEY_SUFFIXES)
