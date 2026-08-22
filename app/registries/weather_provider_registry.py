"""weather_provider_registry.py — static catalog of selectable weather data
sources for Crop Monitoring (sub-analyses E/F). Only 2 fixed providers with
no per-row config worth overriding, so this is a plain static registry (no
DB-override table, unlike `satellite_provider_registry.py` - that one has
real per-row fields, e.g. `gee_collection`, worth admin-editing; this one
doesn't).
"""
from __future__ import annotations

from typing import Any

WEATHER_PROVIDERS: dict[str, dict[str, Any]] = {
    "gee": {
        "label": "Google Earth Engine (CHIRPS + ERA5-Land)",
        "description": "Curah hujan dari CHIRPS Daily, suhu/kelembapan dari ERA5-Land. "
        "Tidak butuh API key eksternal, memakai infrastruktur GEE yang sudah aktif.",
        "needs_key": False,
    },
    "openmeteo": {
        "label": "Open-Meteo",
        "description": "API cuaca eksternal gratis (open-meteo.com), tanpa API key. "
        "Resolusi titik (lat/lon centroid field), bukan rata-rata area polygon.",
        "needs_key": False,
    },
}

DEFAULT_WEATHER_PROVIDER = "gee"


def resolve_weather_provider(key: str | None) -> str:
    if key and key in WEATHER_PROVIDERS:
        return key
    return DEFAULT_WEATHER_PROVIDER


def get_catalog_payload() -> dict[str, Any]:
    return {"providers": WEATHER_PROVIDERS, "default": DEFAULT_WEATHER_PROVIDER}
