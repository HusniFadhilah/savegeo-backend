from datetime import date
from types import SimpleNamespace

from app.registries.disaster_model_registry import DISASTER_MODEL_REGISTRY, list_models
from app.services.disaster_capability_service import check_inputs, model_state


def _event(disaster_type="flood"):
    return SimpleNamespace(id=1, disaster_type=disaster_type)


def _aoi():
    return SimpleNamespace(id=2, event_id=1, geojson={"type": "Polygon"})


def _imagery(phase, satellite, source_kind="gee", resolution_m=10):
    return SimpleNamespace(
        id=phase == "post", event_id=1, phase=phase, satellite=satellite, sensor=None,
        data_source=None, source_kind=source_kind, acquisition_date=date(2026, 1, 1 if phase == "pre" else 2),
        resolution_m=resolution_m, cloud_coverage_pct=None,
    )


def test_all_disaster_types_have_a_capability_entry():
    types = {disaster_type for model in list_models() for disaster_type in model["disaster_types"]}
    assert {"flood", "landslide", "forest_fire", "earthquake", "tsunami", "volcanic_eruption", "storm", "drought", "other"} <= types


def test_sentinel2_model_rejects_blacksky_local_upload():
    result = check_inputs(
        "water_segmentation_v1", _event(), _aoi(),
        _imagery("pre", "Sentinel-2"), _imagery("post", "BlackSky", source_kind="local_upload", resolution_m=1.2),
    )
    assert result.allowed is False
    assert result.status == "incompatible_input"
    assert any("Source kind" in reason or "Sensor imagery" in reason for reason in result.reasons)


def test_dynamic_world_is_not_a_damage_model():
    model = DISASTER_MODEL_REGISTRY["dynamic_world_v1"]
    assert model["damage_model"] is False
    assert model_state(model) == "indicator_only"


def test_other_does_not_enable_a_generic_disaster_model():
    models = [model for model in DISASTER_MODEL_REGISTRY.values() if "other" in model["disaster_types"]]
    assert models
    assert all(not model["enabled"] for model in models)
    assert any(model["result_semantics"] == "not_available" for model in models)


def test_missing_data_is_not_reported_as_zero():
    result = check_inputs("flood_change_v1", _event(), _aoi(), None, None)
    assert result.allowed is False
    assert result.status == "not_available"
    assert "Imagery pre belum tersedia" in result.reasons
    assert "Imagery post belum tersedia" in result.reasons


def test_published_registry_models_have_required_contract_fields():
    required = {
        "model_id", "version", "disaster_types", "category", "result_semantics", "damage_model",
        "validation_status", "required_sources", "accepted_source_kind", "accepted_sensors",
        "required_bands", "supports_local_upload", "requires_pre", "requires_post", "output_type",
        "data_dependencies", "limitations", "publication_requirements",
    }
    for model in DISASTER_MODEL_REGISTRY.values():
        assert required <= model.keys(), model["model_id"]
