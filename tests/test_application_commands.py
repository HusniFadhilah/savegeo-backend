import pytest
from app.agentic.action_catalog import application_action_catalog, classify_application_intent
from app.agentic.agentic_ai import validate_geoai_actions

@pytest.mark.parametrize("message,intent", [
    ("Tampilkan AOI Kota Semarang.", "navigation"),
    ("Cari scene Sentinel-2 terbaru di Semarang dan tampilkan.", "scenes"),
    ("Jalankan klasifikasi tutupan lahan di Semarang.", "landcover"),
    ("Bandingkan tutupan lahan Semarang tahun 2020 dan 2025.", "change"),
    ("Petakan dampak banjir di AOI ini.", "disaster"),
    ("Hitung estimasi stok karbon.", "carbon"),
    ("Mulai pemantauan tanaman di AOI ini.", "crop"),
    ("Analisis apa saja yang tersedia untuk Semarang dan jalankan yang relevan.", "capabilities"),
])
def test_intent(message, intent):
    assert classify_application_intent(message) == intent

def test_catalog_permissions_and_schemas():
    catalog = application_action_catalog({"app.feature.land_cover"})
    assert {"aoi", "landcover"}.issubset({a["id"] for a in catalog})
    assert not any(a["target"] == "carbon.loadOnly" for a in catalog)
    analysis_type = next(a for a in catalog if a["action"] == "set_analysis_type")
    assert analysis_type["parameters"]["properties"]["value"]["enum"] == ["landcover"]
    module = next(a for a in catalog if a["action"] == "open_module")
    assert module["parameters"]["properties"]["value"]["enum"] == ["carbon", "guide", "about"]
    assert catalog[0]["parameters"]["required"] == ["query"]
    assert all(a["data_availability"].startswith("must be checked") for a in catalog)

def test_aoi_then_search_then_latest_is_accepted_before_aoi_exists():
    accepted, warnings = validate_geoai_actions([
        {"action": "select_aoi", "target": "aoi.active", "parameters": {"query": "Kota Semarang"}},
        {"action": "search_scenes", "target": "scenes.searchRequest", "parameters": {}},
        {"action": "show_scene", "target": "scenes.scene", "parameters": {"scene_id": "latest", "visible": True}},
    ], {"has_aoi": False}, {}, {"app.feature.satellite_exploration"})
    assert len(accepted) == 3
    assert not warnings

def test_invalid_aoi_does_not_satisfy_downstream_prerequisite():
    accepted, warnings = validate_geoai_actions([
        {"action": "select_aoi", "target": "aoi.active", "parameters": {"query": ""}},
        {"action": "run_analysis", "target": "carbon.analysis"},
    ], {}, {})
    assert not accepted
    assert len(warnings) == 2

def test_vegetation_execution_uses_selected_type_permissions():
    accepted, warnings = validate_geoai_actions([
        {"action": "set_analysis_type", "target": "carbon.analysisType", "parameters": {"value": "vegetation"}},
        {"action": "run_analysis", "target": "carbon.analysis"},
    ], {"has_aoi": True}, {}, {"app.feature.vegetation_indices"})
    assert len(accepted) == 2
    assert not warnings

def test_field_selection_satisfies_crop_prerequisite():
    accepted, warnings = validate_geoai_actions([
        {"action": "select_field", "target": "crop.field", "parameters": {"field_id": 17}},
        {"action": "run_analysis", "target": "crop.analysis"},
    ], {"ui": {"crop": {"availableFieldIds": [17]}}}, {}, {"app.feature.crop_monitoring"})
    assert len(accepted) == 2
    assert not warnings

@pytest.mark.parametrize("dates,valid", [
    (["2025-01-01", "2025-01-15", "2025-02-01", "2025-02-15"], True),
    (["2025-01-01", "2025-02-15", "2025-02-01", "2025-03-15"], False),
    (["2025-02-30", "2025-03-15", "2025-04-01", "2025-04-15"], False),
])
def test_disaster_periods(dates, valid):
    params = dict(zip(("before_start", "before_end", "after_start", "after_end"), dates))
    params["event_type"] = "flood"
    accepted, warnings = validate_geoai_actions([{"action": "run_analysis", "target": "disaster.event", "parameters": params}],
                                               {"has_aoi": True}, {}, {"app.feature.disaster_mapping"})
    assert bool(accepted) == valid
    assert bool(warnings) != valid

def test_latest_cannot_be_invented_without_search():
    accepted, warnings = validate_geoai_actions([{"action": "show_scene", "target": "scenes.scene",
                                                "parameters": {"scene_id": "latest", "visible": True}}], {}, {})
    assert not accepted
    assert warnings


def test_catalog_describes_parameter_payloads_for_the_planner():
    catalog = application_action_catalog()
    parameters = [a for a in catalog if a["action"] == "set_parameter"]
    assert all(a["parameters"]["required"] == ["value"] for a in parameters)
    assert all(set(a["parameters"]["properties"]) == {"value"} for a in parameters)
    load = next(a for a in parameters if a["target"] == "carbon.loadOnly")
    assert load["parameters"]["properties"]["value"] == {"type": "boolean"}


def test_load_only_reference_workflow_is_accepted():
    actions = [
        {"action": "open_module", "target": "navigation.module", "parameters": {"value": "carbon"}},
        {"action": "set_parameter", "target": "carbon.referenceDataset", "parameters": {"value": "WCMC"}},
        {"action": "set_parameter", "target": "carbon.loadOnly", "parameters": {"value": True}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ]
    accepted, warnings = validate_geoai_actions(actions, {"has_aoi": True, "available_carbon_datasets": ["WCMC"]},
                                               {}, {"app.feature.carbon_stock"})
    assert [a["target"] for a in accepted] == [a["target"] for a in actions]
    assert warnings == []


@pytest.mark.parametrize("value", ["true", 1, None])
def test_load_only_rejects_non_boolean_and_blocks_analysis(value):
    accepted, warnings = validate_geoai_actions([
        {"action": "set_parameter", "target": "carbon.loadOnly", "parameters": {"value": value}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ], {"has_aoi": True}, {})
    assert accepted == []
    assert "carbon.loadOnly" in warnings[0] and "boolean" in warnings[0]
    assert any("Workflow tidak dijalankan" in warning for warning in warnings)


def test_load_only_keeps_feature_permission_and_aoi_requirements():
    command = {"action": "set_parameter", "target": "carbon.loadOnly", "parameters": {"value": True}}
    accepted, warnings = validate_geoai_actions([command], {"has_aoi": True}, {}, {"app.feature.land_cover"})
    assert accepted == []
    assert warnings
    accepted, warnings = validate_geoai_actions([
        command, {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ], {"has_aoi": False}, {}, {"app.feature.carbon_stock"})
    assert [a["action"] for a in accepted] == ["set_parameter"]
    assert any("AOI belum tersedia" in warning for warning in warnings)


def test_unsupported_parameter_names_reveal_target_without_allowing_execution():
    accepted, warnings = validate_geoai_actions([
        {"action": "set_parameter", "target": "carbon.year", "parameters": {"value": 2024}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ], {"has_aoi": True}, {})
    assert accepted == []
    assert "carbon.year" in warnings[0] and "set_parameter" in warnings[0]
    assert "Target" in warnings[0]
    assert any("Workflow tidak dijalankan" in warning for warning in warnings)


def test_ai_response_preserves_load_only_queue_and_requires_confirmation(monkeypatch):
    import json
    from app.agentic import agentic_ai

    monkeypatch.setattr(agentic_ai, "_get_ai_config", lambda: {
        "provider": "opencode", "model": "test-model", "custom_base_url": None,
        "opencode_api_key": None, "opencode_base_url": "http://localhost:9999",
    })
    actions = [
        {"action": "set_parameter", "target": "carbon.loadOnly", "parameters": {"value": True}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ]
    monkeypatch.setattr(agentic_ai, "_call_openai_compat_tools", lambda *args: json.dumps({
        "message": "Layer sudah dimuat.", "actions": actions,
    }))
    response = agentic_ai.geoai_with_ai("Muat layer referensi", {}, None,
                                       page_state={"has_aoi": True}, allowed_features={"app.feature.carbon_stock"})
    assert [a["target"] for a in response["actions"]] == [a["target"] for a in actions]
    assert response["warnings"] == []
    assert response["needs_confirmation"] is True
    assert "sudah dimuat" not in response["message"]


@pytest.mark.parametrize("discriminator", ["action", "type"])
def test_vegetation_queue_accepts_both_discriminators_without_carbon_permission(discriminator):
    commands = [
        ("open_module", "navigation.module", "carbon"),
        ("set_analysis_type", "carbon.analysisType", "vegetation"),
        ("set_parameter", "carbon.analysisYear", 2026),
        ("set_parameter", "carbon.vegetationIndices", ["NDVI", "NDWI", "MNDWI", "NDBI"]),
        ("set_parameter", "carbon.vegetationCloudThreshold", 40),
        ("run_analysis", "carbon.analysis", None),
    ]
    actions = [{discriminator: action, "target": target, "parameters": {"value": value} if value is not None else {}}
               for action, target, value in commands]
    accepted, warnings = validate_geoai_actions(actions, {"has_aoi": True}, {}, {"app.feature.vegetation_indices"})
    assert [a["action"] for a in accepted] == [a[0] for a in commands]
    assert not warnings
    assert accepted[1]["expected_state"] == {"analysisType": "vegetation"}


def test_legacy_discriminator_cannot_bypass_target_or_permission_checks():
    accepted, warnings = validate_geoai_actions([
        {"type": "set_parameter", "target": "carbon.secret", "parameters": {"value": "x"}},
        {"type": "set_parameter", "target": "carbon.loadOnly", "parameters": {"value": True}},
        {"type": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ], {"has_aoi": True, "ui": {"carbon": {"analysisType": "vegetation"}}}, {}, {"app.feature.vegetation_indices"})
    assert not accepted
    assert warnings


def test_application_discriminator_conflicts_are_rejected():
    accepted, warnings = validate_geoai_actions([
        {"action": "unknown", "type": "set_parameter", "target": "carbon.analysisYear", "parameters": {"value": 2026}},
        {"type": [], "target": "carbon.analysisYear", "parameters": {"value": 2026}},
    ], {}, {})
    assert not accepted
    assert len(warnings) == 2


def test_setter_expected_state_is_derived_from_application_contract():
    accepted, warnings = validate_geoai_actions([
        {"action": "set_analysis_type", "target": "carbon.analysisType", "parameters": {"value": "vegetation"},
         "expected_state": {"analysis_type": "vegetation", "hasResult": True}},
        {"action": "set_parameter", "target": "carbon.vegetationCloudThreshold", "parameters": {"value": 30},
         "expected_state": {"cloud": 30}},
    ], {}, {})
    assert not warnings
    assert accepted[0]["expected_state"] == {"analysisType": "vegetation"}
    assert accepted[1]["expected_state"] == {"vegetationCloudThreshold": 30}


def test_vegetation_catalog_exposes_year_and_actual_vegetation_settings():
    targets = {a["target"] for a in application_action_catalog({"app.feature.vegetation_indices"})}
    assert {"carbon.analysisYear", "carbon.vegetationIndices", "carbon.vegetationSensor",
            "carbon.vegetationStartMonth", "carbon.vegetationEndMonth", "carbon.vegetationCloudThreshold"}.issubset(targets)
    assert "carbon.referenceDataset" not in targets


@pytest.mark.parametrize("target,value", [("carbon.vegetationStartMonth", 13), ("carbon.vegetationEndMonth", False),
                                         ("carbon.vegetationCloudThreshold", 101)])
def test_vegetation_temporal_settings_are_validated(target, value):
    accepted, warnings = validate_geoai_actions([
        {"action": "set_parameter", "target": target, "parameters": {"value": value}},
        {"action": "run_analysis", "target": "carbon.analysis", "parameters": {}},
    ], {"has_aoi": True}, {})
    assert not accepted
    assert warnings



def test_supported_analyses_do_not_claim_verified_aoi_coverage():
    analyses = [a for a in application_action_catalog() if a["action"] == "run_analysis"]
    assert analyses
    assert all(a["status"] == "supported" and a["coverage_status"] == "unverified" for a in analyses)
