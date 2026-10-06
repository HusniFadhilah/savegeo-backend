from app.agentic.geoai_tools import execute_geoai_tool, get_current_context


def test_get_analysis_results_all_drops_unmapped_and_disabled_data():
    tool_ctx = {
        "allowed_features": {"app.feature.carbon_stock"},
        "context": {
            "results": {
                "carbon": {"estimate": 12},
                "vegetation": {"ndvi": 0.8},
                "direct": [{"dataset_name": "unmapped"}],
                "unexpected_private_result": {"secret": True},
            }
        },
    }

    results = execute_geoai_tool("get_analysis_results", {"kind": "all"}, tool_ctx)

    assert list(results) == ["carbon"]
    assert results["carbon"]["data"] == {"estimate": 12}


def test_unattributed_direct_analysis_result_is_denied():
    results = execute_geoai_tool(
        "get_analysis_results",
        {"kind": "direct"},
        {"allowed_features": {"app.feature.carbon_stock"}, "context": {"results": {"direct": [{"value": 1}]}}},
    )

    assert results == {"error": "Akses ke data fitur ini tidak diizinkan."}


def test_current_context_hides_disabled_result_and_scene_metadata():
    current = get_current_context(
        {
            "allowed_features": {"app.feature.carbon_stock"},
            "context": {
                "results": {"carbon": {"value": 1}, "vegetation": {"value": 2}, "direct": [1]},
            },
            "page_state": {"ui": {"scenes": {"sceneCount": 4, "selectedSceneId": "scene-1"}}},
        }
    )

    assert current["available_results"] == {"carbon": True}
    assert current["scene_search"] == {}


def test_direct_reference_result_is_scoped_to_carbon_feature():
    carbon_layer = {
        "analysis_kind": "carbon",
        "dataset": "gedi_l4a",
        "dataset_name": "GEDI L4A",
        "effective_year": 2023,
        "resolution": "25 m",
        "provider_type": "GEE",
        "statistics_available": False,
        "tile_url": "https://tiles.example.test/layer?token=private",
        "vis_params": {"min": 0, "max": 100},
    }
    landcover_layer = {
        "analysis_kind": "landcover",
        "dataset": "Dynamic_World",
        "dataset_name": "Dynamic World",
        "effective_year": 2022,
        "statistics_available": False,
    }
    direct_rows = [carbon_layer, landcover_layer]
    carbon_context = {
        "allowed_features": {"app.feature.carbon_stock"},
        "context": {"results": {"direct": direct_rows}},
    }
    result = execute_geoai_tool("get_analysis_results", {"kind": "direct"}, carbon_context)
    assert result["available"] is True
    assert result["data"] == [{key: value for key, value in carbon_layer.items() if key not in {"tile_url", "vis_params"}}]
    assert result["source"] == {
        "source": "direct_reference",
        "dataset": "GEDI L4A",
        "period": 2023,
        "generated_at": None,
    }

    all_results = execute_geoai_tool("get_analysis_results", {"kind": "all"}, carbon_context)
    assert list(all_results) == ["carbon", "direct"]
    assert all_results["direct"]["data"] == result["data"]
    current = get_current_context(carbon_context)
    assert current["available_results"] == {"carbon": False, "direct": True}

    landcover_result = execute_geoai_tool(
        "get_analysis_results",
        {"kind": "direct"},
        {"allowed_features": {"app.feature.land_cover"}, "context": {"results": {"direct": direct_rows}}},
    )
    assert landcover_result["data"] == [landcover_layer]

    combined = execute_geoai_tool(
        "get_analysis_results",
        {"kind": "direct"},
        {
            "allowed_features": {"app.feature.carbon_stock", "app.feature.land_cover"},
            "context": {"results": {"direct": direct_rows}},
        },
    )
    assert combined["source"]["period"] is None

    denied = execute_geoai_tool(
        "get_analysis_results",
        {"kind": "direct"},
        {"allowed_features": {"app.feature.vegetation_indices"}, "context": {"results": {"direct": direct_rows}}},
    )
    assert denied == {"error": "Akses ke data fitur ini tidak diizinkan."}
