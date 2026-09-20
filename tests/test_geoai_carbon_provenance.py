from app.agentic.geoai_tools import query_carbon


def test_reference_only_carbon_is_reported_as_dataset_not_model_prediction():
    carbon = {
        "carbon_estimated": {"inference_mode": "reference_only", "statistics": {"mean": 42}},
        "carbon_reference": {"full_name": "WCMC Carbon Density", "year": 2010,
                             "provider_type": "gee", "target_pool": "AGB"},
        "model_info": {"model_name": "reference_only", "display_mode": "reference_only",
                       "reference_dataset_year": 2010},
        "area_info": {"calculation_area_ha": 10},
    }
    result = query_carbon({"context": {"results": {"carbon": carbon}}})
    assert result["value_type"] == "reference_dataset"
    assert result["reference_dataset"] == "WCMC Carbon Density"
    assert result["reference_dataset_year"] == 2010
    assert result["model_name"] is None
    assert result["source"]["period"] == 2010
