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
    assert {a["id"] for a in catalog} == {"aoi", "landcover"}
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
