"""Optional registry entry for IBM's HLS/GEDI-trained biomass model.

This model is deliberately kept outside carbon_pool_models: it is not a GEE
classifier or a reference-raster regression model and must use its own HLS /
TerraTorch inference path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from app.core.config import Settings, get_settings

MODEL_NAME = "ibm_granite_geospatial_biomass"
TARGET_DATASET = "GEDI_L4A_MONTHLY"
MODEL_VERSION = "1"
MODEL_REPO = "ibm-granite/granite-geospatial-biomass"
MODEL_REVISION = "b472652a3d223d51e888877a1365262687f6906d"
OFFICIAL_CHECKPOINT_SHA256 = "32e91101c75c02c94c7dfa86f48290457812b7a92b7adc903c74953b5fa28b65"
CHECKPOINT_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


@lru_cache(maxsize=4)
def _runtime_has_granite(python: str) -> bool:
    if python == sys.executable:
        try:
            return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("terratorch") is not None
        except (ImportError, ValueError):
            return False
    try:
        result = subprocess.run(
            [python, "-c", "import importlib.util,json; print(json.dumps({m:bool(importlib.util.find_spec(m)) for m in ('torch','terratorch')}))"],
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        )
        modules = json.loads(result.stdout)
        return bool(modules.get("torch") and modules.get("terratorch"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return False


def granite_biomass_paths(settings: Settings | None = None) -> tuple[Path, Path, str] | None:
    settings = settings or get_settings()
    if not settings.ibm_granite_biomass_enabled:
        return None

    checkpoint_text = settings.ibm_granite_biomass_checkpoint_path.strip()
    config_text = settings.ibm_granite_biomass_config_path.strip()
    checksum = settings.ibm_granite_biomass_checkpoint_sha256.strip().lower()
    if checkpoint_text or config_text or checksum:
        # Explicit custom paths retain the strict operator-managed behavior.
        if not checkpoint_text or not config_text or not CHECKPOINT_SHA256_RE.fullmatch(checksum):
            return None
        checkpoint = Path(checkpoint_text).expanduser().resolve()
        config = Path(config_text).expanduser().resolve()
        if not checkpoint.is_file() or not config.is_file():
            return None
    else:
        # Default assets live in persistent model storage and are downloaded
        # from IBM's pinned Hugging Face revision on first inference.
        asset_dir = Path(settings.model_dir).expanduser().resolve() / MODEL_NAME
        checkpoint = asset_dir / "biomass_model.ckpt"
        config = asset_dir / "config.yaml"
        checksum = OFFICIAL_CHECKPOINT_SHA256

    runtime = os.getenv("GRANITE_PYTHON", sys.executable)
    if not _runtime_has_granite(runtime):
        return None
    return checkpoint, config, checksum


def granite_biomass_model(settings: Settings | None = None) -> dict | None:
    """Return a model record when runtime is installed; assets can be fetched later."""
    paths = granite_biomass_paths(settings)
    if paths is None:
        return None
    checkpoint, config, _ = paths
    assets_ready = checkpoint.is_file() and config.is_file()

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
            "runtime_state": "ready" if assets_ready else "download_on_first_use",
            "description": description,
        },
    }
