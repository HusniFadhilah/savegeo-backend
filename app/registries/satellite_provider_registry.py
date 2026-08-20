"""satellite_provider_registry.py — Bootstrap/fallback catalog of selectable
satellite imagery sources (providers) for the vegetation/RGB imagery viewer.

This is now only the *default seed* for the `satellite_providers` DB table
(see app/db/models/satellite_provider_entry.py + app/repositories/
satellite_provider_repo.py) - every field here, including the GEE collection
id and the band-role map, can be overridden per-row in the DB, and a
DB-only row with a key not listed here works too (a brand-new provider added
purely through the database). This module exists so the app still works with
zero DB rows (fresh install, table wiped, etc) and so there is a known-good
reference for what "sentinel2"/"landsat8"/"landsat9" mean by default.

Pure metadata only - no `ee` dependency here (mirrors
vegetation_index_registry.py's separation of concerns). GEE-touching
consumers (standardize_bands, mask_landsat_clouds) live in gee_common.py and
take the resolved band-role map as a plain dict argument, never importing
this registry themselves.
"""
from typing import Any, Dict


# ─────────────────────────────────────────────
# Provider catalog (frontend-facing specs + band-role map)
#
# band_role_map: generic role -> sensor-native band id. Every composite gets
# its bands renamed to the canonical alias column (CANONICAL_ALIAS below,
# matches Sentinel-2's own ids) before calculate_index() runs, so index
# formulas stay sensor-agnostic. A sensor missing a role (e.g. Landsat has no
# red-edge) simply omits that role - vegetation_index_registry.
# available_bands_for_index() already treats a missing band as "skip this
# index for this composite".
# ─────────────────────────────────────────────
SATELLITE_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "sentinel2": {
        "key": "sentinel2",
        "name": "Sentinel-2 (MSI)",
        "provider": "ESA / Copernicus",
        "gee_collection": "COPERNICUS/S2_SR_HARMONIZED",
        "resolution_m": 10,
        "resolution_label": "10 m (RGB/NIR) · 20 m (red-edge/SWIR)",
        "revisit_days": 5,
        "swath_km": 290,
        "launch": "2015 (2A) / 2017 (2B)",
        "start_year": 2015,
        "bands_available": [
            "Coastal Aerosol", "Blue", "Green", "Red", "Red Edge 1", "Red Edge 2",
            "Red Edge 3", "NIR", "NIR Narrow", "Water Vapour", "SWIR1", "SWIR2",
        ],
        "description": "Resolusi tertinggi dan revisit tercepat di antara pilihan gratis - cocok untuk pertanian presisi dan pemantauan vegetasi rutin. Satu-satunya sumber di sini dengan band red-edge.",
        "band_role_map": {
            "blue": "B2", "green": "B3", "red": "B4",
            "re1": "B5", "re2": "B6", "re3": "B7",
            "nir": "B8", "nir_narrow": "B8A",
            "swir1": "B11", "swir2": "B12",
        },
    },
    "landsat8": {
        "key": "landsat8",
        "name": "Landsat 8 (OLI/TIRS)",
        "provider": "USGS / NASA",
        "gee_collection": "LANDSAT/LC08/C02/T1_L2",
        "resolution_m": 30,
        "resolution_label": "30 m (multispektral)",
        "revisit_days": 16,
        "swath_km": 185,
        "launch": "2013",
        "start_year": 2013,
        "bands_available": [
            "Coastal Aerosol", "Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2",
            "Thermal 1", "Thermal 2",
        ],
        "description": "Arsip historis terpanjang di antara pilihan di sini (sejak 2013) - cocok untuk analisis tren jangka panjang. Tidak punya band red-edge.",
        "band_role_map": {
            "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4",
            "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
        },
    },
    "landsat9": {
        "key": "landsat9",
        "name": "Landsat 9 (OLI-2/TIRS-2)",
        "provider": "USGS / NASA",
        "gee_collection": "LANDSAT/LC09/C02/T1_L2",
        "resolution_m": 30,
        "resolution_label": "30 m (multispektral)",
        "revisit_days": 16,
        "swath_km": 185,
        "launch": "2021",
        "start_year": 2021,
        "bands_available": [
            "Coastal Aerosol", "Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2",
            "Thermal 1", "Thermal 2",
        ],
        "description": "Penerus Landsat 8 dengan sensor lebih presisi (radiometrik 14-bit). Dijadwalkan berselang-seling dengan Landsat 8, sehingga revisit gabungan keduanya jadi ~8 hari.",
        "band_role_map": {
            "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4",
            "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
        },
    },
}

DEFAULT_SATELLITE = "sentinel2"

# Target band ids every composite gets renamed to, regardless of source
# sensor (matches Sentinel-2's own naming). This is the one thing that
# genuinely must stay a code constant, not a DB field: calculate_index() in
# gee_common.py has these exact band ids (B4, B8, B11, ...) hardcoded into
# each index formula, so renaming the *targets* would break every formula.
# The *source* side (band_role_map above) is fully DB-overridable.
CANONICAL_ALIAS = {
    "blue": "B2", "green": "B3", "red": "B4",
    "re1": "B5", "re2": "B6", "re3": "B7",
    "nir": "B8", "nir_narrow": "B8A",
    "swir1": "B11", "swir2": "B12",
}


def resolve_satellite(key: str | None) -> str:
    """Fall back to the default provider for an unknown/missing key."""
    return key if key in SATELLITE_PROVIDERS else DEFAULT_SATELLITE
