"""
model_compatibility.py — Model-dataset compatibility helpers.

See: backend/docs/model-registry-strategy.md
"""

# Conservative fallback for legacy models whose name clearly indicates WCMC training.
WCMC_FALLBACK_METADATA = {
    "target_dataset_key": "WCMC",
    "target_pool": "aboveground_biomass_carbon",
    "target_unit": "Mg C/ha",
    "feature_stack": "standard_s2",
    "compatible_reference_datasets": ["WCMC"],
    "gee_deployable": True,
}

# Fallback for models with no clear provenance — blocks cross-dataset use.
UNKNOWN_FALLBACK_METADATA = {
    "target_dataset_key": "UNKNOWN",
    "target_pool": "aboveground_biomass_carbon",
    "compatible_reference_datasets": [],
    "gee_deployable": False,
}


def backfill_legacy_metadata(metadata: dict | None, model_name: str = "") -> dict:
    """
    Conservatively patch metadata for models missing target_dataset_key.

    Rules:
    - If target_dataset_key already present → return as-is.
    - If model name contains "wcmc" → apply WCMC_FALLBACK_METADATA.
    - Otherwise → apply UNKNOWN_FALLBACK_METADATA (safe default, blocks cross-dataset use).

    Existing keys in metadata always win over fallback keys.
    """
    if metadata is None:
        metadata = {}

    if metadata.get("target_dataset_key"):
        return metadata

    name_lower = (model_name or "").lower()
    patch = dict(WCMC_FALLBACK_METADATA) if "wcmc" in name_lower else dict(UNKNOWN_FALLBACK_METADATA)

    # Merge: patch provides defaults, existing metadata keys take precedence.
    return {**patch, **metadata}


def get_compatible_datasets(model_metadata: dict) -> list[str]:
    """
    Return list of reference dataset keys this model is compatible with.

    Explicit compatible_reference_datasets wins.
    Falls back to [target_dataset_key] if not set.
    Returns [] for UNKNOWN models (blocks all validation).
    """
    explicit = model_metadata.get("compatible_reference_datasets")
    if explicit:
        return [d for d in explicit if d and d != "UNKNOWN"]

    target = model_metadata.get("target_dataset_key", "UNKNOWN")
    if target and target != "UNKNOWN":
        return [target]
    return []


def validate_model_dataset_compatibility(
    model_metadata: dict,
    reference_dataset: str,
    require_gee: bool = False,
) -> None:
    """
    Raise ValueError if model cannot be used with reference_dataset.

    Checks:
    1. compatible_reference_datasets includes reference_dataset.
    2. gee_deployable == True if require_gee is True.

    Args:
        model_metadata: dict with target_dataset_key / compatible_reference_datasets / gee_deployable.
        reference_dataset: dataset key from the analysis request (e.g. "WCMC", "GEDI_L4A_MONTHLY").
        require_gee: set True for tile-map inference paths.

    Raises:
        ValueError: on incompatibility.
    """
    model_name = model_metadata.get("model_name") or model_metadata.get("name", "")
    compatible = get_compatible_datasets(model_metadata)

    if not compatible:
        raise ValueError(
            f"Model '{model_name}' has unknown target dataset. "
            f"Cannot validate compatibility with '{reference_dataset}'. "
            "Update model metadata via admin panel (target_dataset_key / compatible_reference_datasets)."
        )

    if reference_dataset not in compatible:
        target = model_metadata.get("target_dataset_key", "unknown")
        raise ValueError(
            f"Model '{model_name}' was trained with '{target}' and is not compatible "
            f"with reference dataset '{reference_dataset}'. "
            f"Compatible datasets: {compatible}."
        )

    if require_gee:
        gee_ok = model_metadata.get("gee_deployable", False)
        gee_type = model_metadata.get("gee_algorithm_type", "")
        # native_classifier is also GEE-tile-capable even though it's a tree model
        if not gee_ok and gee_type not in ("linear_expression", "native_classifier"):
            raise ValueError(
                f"Model '{model_name}' is not GEE-deployable (gee_deployable=false, "
                f"gee_algorithm_type='{gee_type}'). "
                "Use a linear model or run upgrade_gee_native.py to add GEE native support."
            )
