"""Built-in, reproducible BGB/SOC methods; no uploaded pickle is required."""
from copy import deepcopy

from app.registries.carbon_dataset_registry import CARBON_DATASET_REGISTRY


def _model(name, display_name, algorithm, datasets, description, **metadata):
    target = CARBON_DATASET_REGISTRY[datasets[0]]
    return {
        "name": name, "display_name": display_name, "model_type": "carbon", "algorithm": algorithm,
        "version": "1.0", "is_active": True, "description": description,
        "metadata_json": {
            "model_name": name, "algorithm": algorithm, "provider": "builtin_gee", "builtin": True,
            "gee_deployable": True, "gee_algorithm_type": "native_classifier" if algorithm == "Random Forest" else "scientific_equation",
            "target_dataset_key": datasets[0], "compatible_reference_datasets": datasets,
            "target_pool": target["target_pool"], "target_unit": target["unit"],
            "depth_cm": target.get("depth_cm"), "description": description,
            "time_series_supported": False, "accuracy_status": "local_field_validation_required", **metadata,
        },
    }


BUILTIN_CARBON_POOL_MODELS = {}
for _key, _meta in CARBON_DATASET_REGISTRY.items():
    if not _meta.get("pool_analysis"):
        continue
    _name = "pool_rf_" + _key.lower()
    BUILTIN_CARBON_POOL_MODELS[_name] = _model(
        _name, f"Random Forest · {_meta['name']}", "Random Forest", [_key],
        "Random Forest fitted to the selected reference raster and its reference-year Landsat/terrain predictors, "
        "then applied to the selected analysis year's predictors. Spatial holdout metrics measure agreement with "
        "the reference raster; independent field accuracy and annual change accuracy are not established.",
        preliminary=True, training_mode="reference_raster", validation_type="spatial_reference_holdout",
        rf_trees=60, rf_samples=1600, rf_seed=42,
    )

BUILTIN_CARBON_POOL_MODELS["soc_stock_integration_0_30cm"] = _model(
    "soc_stock_integration_0_30cm", "SOC stock · depth integration 0–30 cm", "Soil stock integration",
    ["SOILGRIDS_OCS_0_30CM"],
    "Sum SOC concentration × bulk density × layer thickness × fine-earth fraction for 0-5, 5-15 and 15-30 cm. "
    "Output Mg C/ha. Derived from SoilGrids property means; it can differ from the independently modeled OCS stock map. "
    "Prediction uncertainty of the inputs is not a propagated confidence interval.",
    method="soil_stock_integration", source_properties=["soc_mean", "bdod_mean", "cfvo_mean"],
)

# IPCC 2019 Vol.4 Ch.4 Table 4.4: natural tropical forests in Asia.
# The user selects the ecological domain explicitly via the model, not by inference from greenness.
for _zone, _low, _high in [("rainforest", 0.207, 0.212), ("moist", 0.323, 0.246), ("dry", 0.440, 0.379), ("mountain", 0.322, 0.345)]:
    _name = "bgb_ipcc_asia_" + _zone
    BUILTIN_CARBON_POOL_MODELS[_name] = _model(
        _name, f"IPCC BGB · Asian tropical {_zone} forest", "IPCC root-to-shoot", ["ORNL_BGB"],
        f"IPCC 2019 Table 4.4, natural Asian tropical {_zone} forest: R={_low} for AGB dry mass ≤125 Mg/ha, "
        f"R={_high} above 125 Mg/ha. AGB source: NASA/ORNL 2010; carbon fractions AGB/BGB=0.47. "
        "Hansen tree-cover/loss mask limits output to forest present in 2010; natural origin and ecological domain remain user assumptions. "
        "Not calibrated for cropland, grassland, plantations or outside tropical Asia.",
        method="ipcc_root_to_shoot", ecological_zone=_zone, ratios=[_low, _high], biomass_threshold=125,
        carbon_fractions={"agb": 0.47, "bgb": 0.47},
        source_url="https://www.ipcc-nggip.iges.or.jp/public/2019rf/pdf/4_Volume4/19R_V4_Ch04_Forest%20Land.pdf",
    )


def builtin_carbon_models(target_dataset=None, db=None):
    from app.services.carbon_saved_models import saved_reference_models

    saved = saved_reference_models(target_dataset, db=db) if db is not None else []
    saved_keys = {key for model in saved for key in model["metadata_json"]["compatible_reference_datasets"]}
    return saved + [deepcopy(model) for model in BUILTIN_CARBON_POOL_MODELS.values()
                    if (target_dataset is None or target_dataset in model["metadata_json"]["compatible_reference_datasets"])
                    and not (model["name"].startswith("pool_rf_") and model["metadata_json"]["target_dataset_key"] in saved_keys)]


def get_builtin_carbon_model(name, db=None):
    if (name or "").startswith("reference_"):
        from app.services.carbon_saved_models import saved_reference_models

        saved = saved_reference_models(name=name, db=db)
        return saved[0] if saved else None
    model = BUILTIN_CARBON_POOL_MODELS.get(name or "")
    return deepcopy(model) if model else None
