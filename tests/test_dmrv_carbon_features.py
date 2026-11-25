import pytest

from app.schemas.dmrv_carbon_features import (
    BlueCarbonMonitoringInput,
    BlueCarbonPeriod,
    MethodologyCalculationInput,
    SamplingPlanInput,
    UncertaintyBudgetInput,
    VMD0054LeakageInput,
)
from app.services.dmrv.carbon_features import (
    blue_carbon_monitoring,
    methodology_calculation,
    sampling_plan,
    uncertainty_budget,
    vmd0054_leakage,
)


def test_vmd0054_equation_9_and_13_known_answer():
    data = VMD0054LeakageInput.model_validate({
        "project_start_date": "2025-01-01",
        "monitoring_year": 1,
        "carbon_stock_loss_t_c_ha": 100,
        "commodities": [{
            "commodity": "rice",
            "unit": "tonne",
            "complete_crop_rotation_years": 3,
            "historical_project_production": [100, 105, 95],
            "monitored_project_production": 90,
            "yield_on_new_land_units_per_ha": 5,
            "increased_supply_pct": 50,
            "new_land_pct": 20,
            "evidence": ["document:yield-and-production"],
        }],
        "evidence": ["document:leakage-assessment"],
    })

    result = vmd0054_leakage(data)

    # Mean historical output=100; change=10; area=10*0.50*0.20/5=0.2 ha.
    # Leakage=0.2*100*44/12=73.333... tCO2e.
    assert result["commodities"][0]["baseline_production"] == pytest.approx(100)
    assert result["commodities"][0]["new_land_area_impact_ha"] == pytest.approx(0.2)
    assert result["land_conversion_leakage_tco2e"] == pytest.approx(73.3333333333)
    assert result["total_leakage_tco2e"] == pytest.approx(73.3333333333)


def test_vmd0054_does_not_credit_net_land_sparing():
    data = VMD0054LeakageInput.model_validate({
        "project_start_date": "2025-01-01",
        "monitoring_year": 5,
        "carbon_stock_loss_t_c_ha": 100,
        "commodities": [{
            "commodity": "rice",
            "unit": "tonne",
            "complete_crop_rotation_years": 3,
            "historical_project_production": [100, 100, 100],
            "monitored_project_production": 110,
            "yield_on_new_land_units_per_ha": 5,
            "increased_supply_pct": 75,
            "new_land_pct": 40,
            "evidence": ["document:production"],
        }],
        "evidence": ["document:leakage-assessment"],
    })

    result = vmd0054_leakage(data)

    assert result["net_land_impact_before_floor_ha"] < 0
    assert result["area_generating_leakage_ha"] == 0
    assert result["total_leakage_tco2e"] == 0


def test_vmd0054_cross_commodity_uses_zero_baseline_and_method_defaults():
    data = VMD0054LeakageInput.model_validate({
        "project_start_date": "2025-01-01",
        "monitoring_year": 1,
        "cross_commodity_accounting": True,
        "carbon_stock_loss_t_c_ha": 100,
        "commodities": [
            {
                "commodity": "rice", "unit": "tonne", "complete_crop_rotation_years": 3,
                "historical_project_production": [100, 100, 100], "monitored_project_production": 90,
                "yield_on_new_land_units_per_ha": 5, "evidence": ["document:rice-production"],
            },
            {
                "commodity": "maize", "unit": "tonne", "introduced_by_project": True,
                "national_production_increasing_evidence": ["document:national-trend"],
                "monitored_project_production": 5, "yield_on_new_land_units_per_ha": 5,
                "evidence": ["document:maize-production"],
            },
        ],
        "evidence": ["document:cross-commodity-assessment"],
    })

    result = vmd0054_leakage(data)

    introduced = result["commodities"][1]
    assert introduced["baseline_production"] == 0
    assert introduced["increased_supply_pct"] == 100
    assert introduced["new_land_pct"] == 100
    assert result["net_land_impact_before_floor_ha"] == pytest.approx(1)
    assert result["total_leakage_tco2e"] == pytest.approx(366.6666666667)


def test_uncertainty_derives_correlation_from_shared_field_ids():
    data = UncertaintyBudgetInput.model_validate({
        "central_net_tco2e": 1000,
        "components": [
            {"source": "model", "observations_tco2e": [1, 2, 3], "observation_ids": ["a", "b", "c"], "evidence": ["document:model-residuals"]},
            {"source": "field_validation", "observations_tco2e": [2, 4, 6], "observation_ids": ["a", "b", "c"], "evidence": ["document:field-validation"]},
        ],
        "simulation_draws": 2000,
        "seed": 42,
    })

    result = uncertainty_budget(data)

    assert result["correlations_derived_from_shared_validation_ids"][0]["correlation"] == pytest.approx(1)
    assert result["combined_standard_uncertainty_tco2e"] == pytest.approx(3)
    assert result["interval_tco2e"][0] < 1000 < result["interval_tco2e"][1]


def test_blue_carbon_baseline_relative_series_and_missing_inputs_are_explicit():
    periods = [
        {
            "year": year,
            "habitat": "mangrove",
            "habitat_area_ha": 10,
            "biomass_mg_c_ha": biomass,
            "sediment_soc_mg_c_ha": soc,
            "baseline_habitat_area_ha": 10,
            "baseline_biomass_mg_c_ha": 50,
            "baseline_sediment_soc_mg_c_ha": 300,
            "ch4_emissions_tco2e_yr": 1,
            "n2o_emissions_tco2e_yr": 0.5,
            "baseline_ch4_emissions_tco2e_yr": 1,
            "baseline_n2o_emissions_tco2e_yr": 0.5,
            "source": "field inventory",
        }
        for year, biomass, soc in [(2020, 60, 310), (2025, 70, 320)]
    ]
    result = blue_carbon_monitoring(BlueCarbonMonitoringInput.model_validate({"periods": periods}))

    assert result["baseline_relative_additional_stock_change_tco2e"] == pytest.approx(733.3333333333)
    assert result["baseline_relative_net_change_tco2e"] == pytest.approx(733.3333333333)
    assert result["warnings"]  # uncertainty and 100-year method requirements remain explicit


def test_sampling_plan_is_reproducible_and_creates_plot_ids():
    polygon = {
        "type": "Polygon",
        "coordinates": [[[110, -7], [110.01, -7], [110.01, -6.99], [110, -6.99], [110, -7]]],
    }
    data = SamplingPlanInput.model_validate({
        "confidence_level_pct": 90,
        "target_precision_pct": 50,
        "seed": 42,
        "strata": [{"name": "mangrove", "area_ha": 1, "expected_cv_pct": 10, "geometry": polygon}],
        "pools": ["AGB", "SOC"],
    })

    first = sampling_plan(data)
    second = sampling_plan(data)

    assert first["recommended_plot_count"] == 1
    assert first["strata"][0]["plot_locations"] == second["strata"][0]["plot_locations"]
    assert first["measurement_checklist"]["SOC"]


def test_methodology_input_enforces_census_leakage_and_density_rules():
    invalid = {
        "approach": "census_based",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "area_ha": 1,
        "census_planting_units": 100,
        "census_sampled_units": 20,
        "census_surviving_sampled_units": 18,
        "census_mean_live_unit_mg_c": 0.1,
        "planting_density_per_ha": 51,
        "census_land_use_unchanged": True,
        "leakage_tco2e": 1,
    }
    with pytest.raises(ValueError):
        MethodologyCalculationInput.model_validate(invalid)


def test_area_based_benchmark_uses_two_sided_z_and_endpoint_cumulative_stock():
    data = MethodologyCalculationInput.model_validate({
        "approach": "area_based",
        "period_start": "2020-01-01",
        "period_end": "2022-12-31",
        "area_ha": 1,
        "leakage_assessment_basis": "reviewed zero leakage for illustrative test",
        "assessment_evidence_complete": True,
        "evidence": ["document:benchmark-fixture"],
        "pools": [{
            "pool": "AGB",
            "start_mg_c_ha": 0,
            "end_mg_c_ha": 10,
            "cumulative_start_change_mg_c_ha": 0,
            "cumulative_end_change_mg_c_ha": 10,
        }],
        "plots": [{
            "plot_id": f"plot-{index}",
            "years": [2020, 2021, 2022],
            "project_si": [0, 2, 4],
            "control_si": [0, 1, 2],
            "weight": 1,
        } for index in range(30)],
    })

    result = methodology_calculation(data)

    assert result["benchmark"]["at_monitoring_period_end"]["significance_threshold_abs_z"] == 1.96
    assert result["benchmark"]["performance_benchmark_ratio"] == pytest.approx(0.5)
    assert result["gross_screening_tco2e"] == pytest.approx(18.3333333333)
