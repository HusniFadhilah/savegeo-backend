"""Optional registry entry for IBM's HLS/GEDI-trained biomass model.

This model is deliberately kept outside carbon_pool_models: it is not a GEE
classifier or a reference-raster regression model and must use its own HLS /
TerraTorch inference path.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from app.core.config import Settings, get_settings

MODEL_NAME = "ibm_granite_geospatial_biomass"
TARGET_DATASET = "GEDI_L4A_MONTHLY"
MODEL_VERSION = "1"
CHECKPOINT_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def granite_biomass_paths(settings: Settings | None = None) -> tuple[Path, Path, str] | None:
    settings = settings or get_settings()
    if not settings.ibm_granite_biomass_enabled:
        return None

    checkpoint_text = settings.ibm_granite_biomass_checkpoint_path.strip()
    config_text = settings.ibm_granite_biomass_config_path.strip()
    checksum = settings.ibm_granite_biomass_checkpoint_sha256.strip().lower()
    if not checkpoint_text or not config_text or not CHECKPOINT_SHA256_RE.fullmatch(checksum):
        return None

    checkpoint = Path(checkpoint_text).expanduser().resolve()
    config = Path(config_text).expanduser().resolve()
    if not checkpoint.is_file() or not config.is_file():
        return None
    try:
        if importlib.util.find_spec("torch") is None or importlib.util.find_spec("terratorch") is None:
            return None
    except (ImportError, ValueError):
        return None
    return checkpoint, config, checksum


def granite_biomass_model(settings: Settings | None = None) -> dict | None:
    """Return a public-compatible model record only when runtime is complete."""
    if granite_biomass_paths(settings) is None:
        return None

    description = (
        "IBM Granite geospatial biomass v1 estimates above-ground biomass from a 30 m HLS L30 composite. "
        "It was fine-tuned with GEDI L4A observations across 15 biomes. SAVEGEO converts AGB to carbon "
        "density using 0.47. Experimental for Indonesia; this is not locally validated and is not an "
        "annual carbon-change model."
    )
    return {
        "name": MODEL_NAME,
        "display_name": "IBM Granite Geospatial Biomass · HLS",
        "model_type": "carbon",
        "algorithm": "TerraTorch pixel-wise regression",
        "version": MODEL_VERSION,
        "is_active": True,
        "description": description,
        "metrics": None,
        "metadata_json": {
            "model_name": MODEL_NAME,
            "algorithm": "TerraTorch pixel-wise regression",
            "provider": "ibm_granite_hls",
            "gee_deployable": False,
            "experimental": True,
            "preliminary": True,
            "target_dataset_key": TARGET_DATASET,
            "compatible_reference_datasets": [TARGET_DATASET],
            "target_pool": "aboveground_biomass_carbon",
            "target_unit": "Mg C/ha",
            "time_series_supported": False,
            "training_reference": "NASA GEDI L4A",
            "input_dataset": "NASA HLS L30 v2",
            "input_bands": ["BLUE", "GREEN", "RED", "NIR_NARROW", "SWIR_1", "SWIR_2"],
            "input_resolution_m": 30,
            "carbon_fraction": 0.47,
            "source_url": "https://huggingface.co/ibm-granite/granite-geospatial-biomass",
            "license": "Apache-2.0",
            "accuracy_status": "global_zero_shot; local_field_validation_required",
            "description": description,
        },
    }
