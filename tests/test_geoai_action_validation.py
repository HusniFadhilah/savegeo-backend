from app.agentic.agentic_ai import validate_geoai_actions


def test_rejects_unknown_targets_and_analysis_without_aoi():
    actions = [
        {"action": "set_parameter", "target": "carbon.secret", "parameters": {"value": "x"}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ]
    accepted, warnings = validate_geoai_actions(actions, {"has_aoi": False}, {})
    assert accepted == []
    assert len(warnings) == 2


def test_accepts_valid_control_and_rejects_unavailable_dataset():
    actions = [
        {"action": "set_parameter", "target": "carbon.referenceDataset", "parameters": {"value": "missing"}},
        {"action": "set_parameter", "target": "carbon.analysisYear", "parameters": {"value": 2024},
         "expected_state": {"analysisYear": 2024}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ]
    accepted, warnings = validate_geoai_actions(
        actions, {"available_carbon_datasets": ["WCMC"], "has_aoi": True}, {"aoi": {"type": "Polygon"}}
    )
    assert [a["action"] for a in accepted] == ["set_parameter", "run_analysis"]
    assert accepted[0]["expected_state"] == {"analysisYear": 2024}
    assert len(warnings) == 1


def test_rejects_invalid_temporal_and_map_inputs():
    actions = [
        {"action": "set_parameter", "target": "lc_change.fromYear", "parameters": {"value": True}},
        {"action": "set_parameter", "target": "carbon.startMonth", "parameters": {"value": 13}},
        {"action": "set_map_view", "target": "map.main", "parameters": {"center": [200, 118], "zoom": 8}},
    ]
    accepted, warnings = validate_geoai_actions(actions, {"has_aoi": True}, {})
    assert accepted == []
    assert len(warnings) == 3


def test_layer_visibility_requires_a_layer_in_current_state():
    accepted, warnings = validate_geoai_actions([
        {"action": "toggle_layer", "target": "results.layer", "parameters": {"visible": True, "value": "missing"}},
        {"action": "toggle_layer", "target": "results.layer", "parameters": {"visible": True, "value": "carbon_estimated"}},
    ], {"ui": {"results": {"availableLayers": ["carbon_estimated"]}}}, {})
    assert len(accepted) == 1
    assert accepted[0]["parameters"]["value"] == "carbon_estimated"
    assert len(warnings) == 1


def test_crop_analysis_uses_selected_field_instead_of_map_aoi():
    command = {"action": "run_analysis", "target": "crop.analysis", "parameters": {}}
    accepted, warnings = validate_geoai_actions([command], {"ui": {"crop": {"fieldId": 17}}}, {})
    assert len(accepted) == 1
    assert warnings == []
    accepted, warnings = validate_geoai_actions([command], {"ui": {"crop": {"fieldId": None}}}, {})
    assert accepted == []
    assert len(warnings) == 1


def test_direct_carbon_can_run_without_aoi_after_mode_is_set():
    accepted, warnings = validate_geoai_actions([
        {"action": "set_parameter", "target": "carbon.directGlobal", "parameters": {"value": True}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ], {"has_aoi": False}, {})
    assert [action["action"] for action in accepted] == ["set_parameter", "run_analysis"]
    assert warnings == []
