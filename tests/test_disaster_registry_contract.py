from app.registries.disaster_model_registry import DISASTER_MODEL_REGISTRY


def test_required_disaster_product_models_are_registered_with_explicit_semantics():
    expected = {
        "flood_change_v2": "inundation_change_indicator",
        "water_segmentation_v2": "water_extent",
        "flood_building_exposure_v1": "building_exposure",
        "flood_road_exposure_v1": "road_exposure",
        "flood_landcover_impact_v1": "landcover_impact",
        "earthquake_building_change_v2": "building_change_candidate",
        "earthquake_building_segmentation_v1": "building_footprint",
        "earthquake_damage_candidate_v1": "candidate_damage",
        "earthquake_road_impact_v1": "road_exposure",
        "earthquake_intensity_v1": "official_intensity",
    }
    required = {
        "model_id", "version", "user_label", "category", "result_semantics", "damage_model",
        "accepted_sensors", "accepted_source_kind", "requires_pre", "requires_post", "output_type",
        "resolution_m", "data_dependencies", "limitations", "publication_requirements",
        "validation_status", "requires_earth_engine",
    }
    for model_id, semantics in expected.items():
        model = DISASTER_MODEL_REGISTRY[model_id]
        assert model["result_semantics"] == semantics
        assert required <= model.keys()
        if "earthquake" in model["disaster_types"]:
            assert model["damage_model"] is False
            assert model["validation_status"] in {"review_required", "method_validation_only"}


def test_unavailable_earthquake_products_do_not_look_like_confirmed_damage():
    for model_id in ("earthquake_building_change_v2", "earthquake_damage_candidate_v1"):
        model = DISASTER_MODEL_REGISTRY[model_id]
        assert model["enabled"] is False
        assert model["damage_model"] is False
