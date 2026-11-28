"""Stable content identity for code-defined Earth Engine analysis serving."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.core.config import get_settings


_SERVING_SOURCE_FILES = (
    "app/services/carbon_service.py",
    "app/services/carbon_pool_service.py",
    "app/services/carbon_saved_models.py",
    "app/services/detritus_training.py",
    "app/services/carbon_extensions.py",
    "app/registries/carbon_dataset_registry.py",
    "app/registries/carbon_pool_models.py",
    "app/services/gee_common.py",
    "app/services/landcover_service.py",
    "app/services/landcover_model_service.py",
    "app/services/landcover_fusion_service.py",
    "app/services/geosave_landcover_service.py",
    "app/services/vegetation_service.py",
    "app/registries/landcover_dataset_registry.py",
    "app/registries/vegetation_index_registry.py",
)


def current_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative in _SERVING_SOURCE_FILES:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\\0")
        digest.update(path.read_bytes() if path.is_file() else b"missing")
        digest.update(b"\\0")
    landcover_registry_path = Path(get_settings().landcover_model_config_path).resolve()
    landcover_registry_sha256 = (
        hashlib.sha256(landcover_registry_path.read_bytes()).hexdigest()
        if landcover_registry_path.is_file()
        else hashlib.sha256(b"missing").hexdigest()
    )
    # This identity is persisted on queued GEE jobs and checked later by a
    # worker. Keep it limited to serving inputs that must remain identical.
    # Deployment metadata is written to the shared /app/var volume after the
    # new API and workers start, so commit_sha/product_version can change while
    # a job is waiting even though the serving code itself did not change.
    return {
        "serving_code_sha256": digest.hexdigest(),
        "landcover_registry_sha256": landcover_registry_sha256,
    }


def matches(expected: dict[str, Any] | None) -> bool:
    if not isinstance(expected, dict):
        return False
    actual = current_identity()
    # Compare only semantic serving identity fields. This remains compatible
    # with queued jobs created by older releases, whose identity also carried
    # volatile deployment metadata (commit and product version).
    return all(expected.get(key) == value for key, value in actual.items())
