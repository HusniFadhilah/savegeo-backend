"""Reproducible screening calculations used by the dMRV carbon workflows.

These outputs are reviewer aids. They never establish methodology eligibility,
verification, or issuance. Method-specific inputs and unsupported elements are
kept visible in the result so the project can complete its VVB review.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist, mean, stdev

from app.schemas.dmrv_carbon_features import (
    BlueCarbonMonitoringInput,
    CommunityBenefitInput,
    MethodologyCalculationInput,
    VMD0054LeakageInput,
    RegistryReconciliationInput,
    SafeguardIndicatorInput,
    SamplingPlanInput,
    UncertaintyBudgetInput,
)


def _slope(values: list[float]) -> float:
    n = len(values)
    xbar = (n - 1) / 2
    ybar = mean(values)
    denom = sum((i - xbar) ** 2 for i in range(n))
    return sum((i - xbar) * (y - ybar) for i, y in enumerate(values)) / denom


def _weighted_regression_slope(years: list[int], values: list[float], weights: list[float]) -> tuple[float, float]:
    """Weighted linear trend and standard error for a screening time series."""
    n = len(values)
    if n != len(years) or n != len(weights) or n < 3:
        raise ValueError("At least three aligned annual observations are required for a trend.")
    weight_total = sum(weights)
    if weight_total <= 0:
        raise ValueError("Regression weights must sum to a positive value.")
    # Normalize the total weight so the residual variance is not changed by an
    # arbitrary common scale in the user-provided matching weights.
    normalized = [weight * n / weight_total for weight in weights]
    x_bar = sum(w * x for w, x in zip(normalized, years)) / n
    y_bar = sum(w * y for w, y in zip(normalized, values)) / n
    sxx = sum(w * (x - x_bar) ** 2 for w, x in zip(normalized, years))
    if sxx <= 0:
        raise ValueError("Annual observations must span at least two different years.")
    slope = sum(w * (x - x_bar) * (y - y_bar) for w, x, y in zip(normalized, years, values)) / sxx
    intercept = y_bar - slope * x_bar
    residual_sum = sum(w * (y - (intercept + slope * x)) ** 2 for w, x, y in zip(normalized, years, values))
    standard_error = math.sqrt(max(0.0, residual_sum / (n - 2) / sxx))
    return slope, standard_error


def methodology_calculation(data: MethodologyCalculationInput) -> dict:
    warnings: list[str] = []
    pool_lines = []
    gross_c = 0.0
    pool_uncertainty_variance_c = 0.0
    census_stock_se_mg_c = None
    census_period_se_mg_c = None
    benchmark = None
    census_current_stock_mg_c = None
    area_based_ready = True
    period_delta_c = 0.0
    methodology_uncertainty_basis = "pool stock change uncertainty only"
    if data.approach == "area_based":
        years = data.plots[0].years
        n = len(data.plots)
        if not years:
            years = list(range(len(data.plots[0].project_si)))
            warnings.append("calendar_years_missing_synthetic_time_index_used")

        def benchmark_at(index: int) -> dict | None:
            if index < 2:
                return None
            x = years[: index + 1]
            project_values = [plot.project_si[: index + 1] for plot in data.plots]
            control_values = [plot.control_si[: index + 1] for plot in data.plots]
            project_slope, project_se = _weighted_regression_slope(
                [year for _ in data.plots for year in x],
                [value for series in project_values for value in series],
                [1.0 for _ in range(len(data.plots) * len(x))],
            )
            control_slope, control_se = _weighted_regression_slope(
                [year for _ in data.plots for year in x],
                [value for series in control_values for value in series],
                [plot.weight for plot in data.plots for _ in x],
            )
            difference_se = math.sqrt(project_se ** 2 + control_se ** 2)
            difference = project_slope - control_slope
            z_score = (difference / difference_se if difference_se > 0 else
                       (1e12 if difference > 0 else (-1e12 if difference < 0 else 0.0)))
            significant = abs(z_score) >= 1.96
            control_for_benchmark = control_slope if control_slope > 1.96 * control_se and control_slope > 0 else 0.0
            if significant and abs(project_slope) > 1e-12:
                pb = control_for_benchmark / project_slope
            elif significant:
                pb = None
            else:
                pb = 1.0
            return {
                "project_si_slope_per_year": project_slope,
                "project_si_slope_standard_error": project_se,
                "control_si_slope_per_year": control_slope,
                "control_si_slope_standard_error": control_se,
                "z_score": z_score,
                "significance_threshold_abs_z": 1.96,
                "slopes_significantly_different": significant,
                "control_slope_used_for_benchmark": control_for_benchmark,
                "performance_benchmark": pb,
                "years_used": x,
            }

        start_index = years.index(data.period_start.year) if data.period_start.year in years else None
        end_index = years.index(data.period_end.year) if data.period_end.year in years else None
        if end_index is None:
            warnings.append("period_end_year_not_present_in_stocking_index_series")
            area_based_ready = False
            benchmark_end = None
        else:
            benchmark_end = benchmark_at(end_index)
            if benchmark_end is None or benchmark_end["performance_benchmark"] is None:
                warnings.append("performance_benchmark_at_period_end_not_computable")
                area_based_ready = False
        if start_index is None:
            warnings.append("period_start_year_not_present_in_stocking_index_series")
            benchmark_start = None
            area_based_ready = False
        else:
            benchmark_start = benchmark_at(start_index)
        if n < 30:
            warnings.append("minimum_30_area_based_project_plots_not_met")
            area_based_ready = False
        if benchmark_end:
            benchmark = benchmark_end["performance_benchmark"]
        else:
            benchmark = None
        if not data.leakage_assessment_basis.strip() or not data.evidence:
            warnings.append("area_based_vmd0054_leakage_assessment_or_evidence_missing")
        warnings.extend([
            "vmd0054_leakage_amount_is_project_input_unless_reviewed_module_record_is_linked",
            "methodology_uncertainty_deduction_not_applied",
            "control_plot_matching_donor_pool_and_annual_cohort_eligibility_require_reviewer_confirmation",
        ])
        if not all(years[index] + 1 == years[index + 1] for index in range(len(years) - 1)):
            warnings.append("stocking_index_series_must_be_annual_without_gaps_for_method_review")
            area_based_ready = False
        if benchmark_end and benchmark_end["project_si_slope_per_year"] <= 0:
            warnings.append("project_stocking_index_slope_not_positive")
        for pool in data.pools:
            if pool.cumulative_start_change_mg_c_ha is None or pool.cumulative_end_change_mg_c_ha is None:
                warnings.append(f"{pool.pool.lower()}_cumulative_start_and_end_changes_required_for_interval_screening")
                area_based_ready = False
                delta = None
            else:
                start_c = pool.cumulative_start_change_mg_c_ha
                end_c = pool.cumulative_end_change_mg_c_ha
                period_start_pb = (benchmark_start or {}).get("performance_benchmark")
                if abs(start_c) <= 1e-12:
                    period_start_pb = 0.0
                if period_start_pb is None or benchmark is None:
                    warnings.append(f"{pool.pool.lower()}_endpoint_benchmark_missing")
                    area_based_ready = False
                    delta = None
                else:
                    adjusted_start = start_c * (1.0 - period_start_pb)
                    adjusted_end = end_c * (1.0 - benchmark)
                    delta = adjusted_end - adjusted_start
                    # The benchmark is treated as fixed, consistent with the
                    # method's zero-uncertainty assumption for PB. This interval
                    # SE remains supplemental and is not the Eq. 28 deduction.
                    start_factor = 1.0 - period_start_pb
                    end_factor = 1.0 - benchmark
                    variance_ha = max(0.0, (pool.start_se_mg_c_ha * start_factor) ** 2
                                      + (pool.end_se_mg_c_ha * end_factor) ** 2
                                      - 2 * pool.temporal_correlation * pool.start_se_mg_c_ha
                                      * start_factor * pool.end_se_mg_c_ha * end_factor)
                    pool_uncertainty_variance_c += (variance_ha * data.area_ha ** 2)
                    gross_c += delta * data.area_ha
                    period_delta_c += (end_c - start_c) * data.area_ha
            pool_lines.append({
                "pool": pool.pool,
                "cumulative_interval_stock_change_mg_c_ha": delta,
                "performance_benchmark_start": (benchmark_start or {}).get("performance_benchmark"),
                "performance_benchmark_end": benchmark,
                "additional_change_mg_c": delta * data.area_ha if delta is not None else None,
                "supplemental_interval_standard_uncertainty_mg_c": math.sqrt(variance_ha) * data.area_ha if delta is not None else None,
                "source": "user-supplied cumulative pool inventories; matched SI trend screening",
            })
        benchmark_result = {
            "at_monitoring_period_start": benchmark_start,
            "at_monitoring_period_end": benchmark_end,
            "performance_benchmark_ratio": benchmark,
            "matched_project_control_plot_count": n,
            "years_used": years,
            "calculation_note": "SaveGeo one-control-per-project-plot weighted trend screen. It uses a two-sided |Z| >= 1.96 test and zeroes an insignificant or negative control slope; it does not perform Verra's donor-pool matching or replace the full A1-A8 calculation.",
        }
        if benchmark_end and not benchmark_end["slopes_significantly_different"]:
            warnings.append("performance_benchmark_set_to_one_slopes_not_significantly_different")
    else:
        sample_n = data.census_sampled_units or 0
        live_n = data.census_surviving_sampled_units or 0
        planted_n = data.census_planting_units or 0
        survival = live_n / sample_n if sample_n else 0.0
        estimated_live_units = planted_n * survival
        mean_unit_c = data.census_mean_live_unit_mg_c or 0.0
        census_stock_mg_c = estimated_live_units * mean_unit_c
        census_current_stock_mg_c = census_stock_mg_c
        survival_se = math.sqrt(survival * (1 - survival) / sample_n) if sample_n else 0.0
        mean_unit_se = data.census_mean_live_unit_se_mg_c
        if mean_unit_se is not None:
            census_stock_se_mg_c = math.sqrt((estimated_live_units * mean_unit_se) ** 2 +
                                              (planted_n * mean_unit_c * survival_se) ** 2)
        gross_c = census_stock_mg_c
        census_delta_mg_c = None
        if data.census_start_live_units is not None and data.census_start_mean_live_unit_mg_c is not None:
            census_start_stock = data.census_start_live_units * data.census_start_mean_live_unit_mg_c
            census_delta_mg_c = census_stock_mg_c - census_start_stock
            gross_c = census_delta_mg_c
            if data.census_start_mean_live_unit_se_mg_c is not None and data.census_mean_live_unit_se_mg_c is not None:
                census_period_se_mg_c = math.sqrt(
                    (estimated_live_units * data.census_mean_live_unit_se_mg_c) ** 2
                    + (data.census_start_live_units * data.census_start_mean_live_unit_se_mg_c) ** 2
                    + (planted_n * mean_unit_c * survival_se) ** 2
                )
            else:
                warnings.append("census_repeat_measurement_uncertainty_incomplete")
            methodology_uncertainty_basis = "current and previous census unit biomass SE; survival sampling error"
        else:
            warnings.append("repeat_census_required_for_period_removals")
        if not data.census_direct_planting:
            warnings.append("census_direct_planting_evidence_not_confirmed")
        if data.census_preexisting_woody_cover_pct is None or data.census_preexisting_woody_cover_pct >= 10:
            warnings.append("census_preexisting_woody_cover_eligibility_not_demonstrated")
        if data.census_site_prep_soil_disturbance is None:
            warnings.append("census_site_preparation_status_not_assessed")
        elif data.census_site_prep_soil_disturbance:
            warnings.append("census_site_preparation_or_soil_disturbance_requires_method_review")
        benchmark_result = {
            "planting_density_per_ha": data.planting_density_per_ha,
            "sampled_planting_units": sample_n,
            "surviving_sampled_units": live_n,
            "estimated_survival_fraction": survival,
            "estimated_live_planting_units": estimated_live_units,
            "estimated_current_stock_mg_c": census_stock_mg_c,
            "estimated_current_stock_standard_uncertainty_mg_c": census_stock_se_mg_c,
            "estimated_period_stock_change_mg_c": census_delta_mg_c,
            "estimated_period_stock_change_standard_uncertainty_mg_c": census_period_se_mg_c,
            "baseline_assumption": "zero, subject to applicable VM0047 census criteria and evidence",
            "eligibility_screen": {
                "direct_planting_confirmed": data.census_direct_planting,
                "land_use_unchanged": data.census_land_use_unchanged,
                "preexisting_woody_cover_pct": data.census_preexisting_woody_cover_pct,
                "soil_disturbance_reported": data.census_site_prep_soil_disturbance,
                "planting_density_per_ha": data.planting_density_per_ha,
            },
        }
        if data.census_mean_live_unit_se_mg_c is None:
            warnings.append("census_biomass_sampling_uncertainty_not_provided")
        if data.leakage_tco2e:
            warnings.append("census_leakage_input_retained_for_review; confirm_methodology_applicability")
    gross_tco2e = gross_c * 44 / 12 if area_based_ready or (data.approach == "census_based" and benchmark_result.get("estimated_period_stock_change_mg_c") is not None) else None
    standard_uncertainty_tco2e = math.sqrt(pool_uncertainty_variance_c) * 44 / 12 if data.approach == "area_based" else (census_period_se_mg_c * 44 / 12 if census_period_se_mg_c is not None else None)
    interval_half_width_tco2e = 1.645 * standard_uncertainty_tco2e if standard_uncertainty_tco2e is not None and area_based_ready else None
    net_screening = gross_tco2e - data.project_emissions_tco2e - data.leakage_tco2e if gross_tco2e is not None else None
    if not data.assessment_evidence_complete:
        warnings.append("methodology_eligibility_evidence_checklist_incomplete")
    return {
        "algorithm": "savegeo_vm0047_v1_1_weighted_trend_period_change_screening_v2",
        "methodology": {"id": data.methodology_id, "version": data.methodology_version, "approach": data.approach},
        "leakage_module": {
            "id": "VMD0054",
            "version": "1.1" if data.approach == "area_based" else None,
            "status": "project_input_and_evidence_required" if data.approach == "area_based" else "not_applicable_for_census_subject_to_eligibility",
            "source": "https://verra.org/methodologies/vmd0054-estimating-leakage-from-the-displacement-of-agricultural-activities-v1-1/",
        },
        "period": {"start": data.period_start.isoformat(), "end": data.period_end.isoformat()},
        "area_ha_used": data.area_ha,
        "benchmark": benchmark_result,
        "pools": pool_lines,
        "gross_screening_tco2e": gross_tco2e if data.approach == "area_based" else None,
        "current_census_stock_tco2e": census_current_stock_mg_c * 44 / 12 if census_current_stock_mg_c is not None else None,
        "net_screening_tco2e": net_screening,
        "measurement_standard_uncertainty_tco2e": standard_uncertainty_tco2e,
        "measurement_interval_90_tco2e": [gross_tco2e - interval_half_width_tco2e, gross_tco2e + interval_half_width_tco2e] if interval_half_width_tco2e is not None else None,
        "project_emissions_tco2e": data.project_emissions_tco2e,
        "leakage_tco2e": data.leakage_tco2e,
        "leakage_assessment_basis": data.leakage_assessment_basis,
        "uncertainty_basis": methodology_uncertainty_basis,
        "period_stock_change_before_benchmark_tco2e": period_delta_c * 44 / 12 if data.approach == "area_based" else (benchmark_result.get("estimated_period_stock_change_mg_c", 0) or 0) * 44 / 12,
        "calculation_status": "review_required" if warnings else "screening_complete",
        "warnings": warnings,
        "qualification": "SaveGeo screening calculation, not the Verra Digitalized Methodology. Inputs remain project-reported; formula implementation, pool eligibility, census criteria, leakage, and uncertainty must be checked against the current controlled methodology by a qualified VVB before any claim or issuance.",
        "source": "https://verra.org/methodologies/vm0047-afforestation-reforestation-and-revegetation-v1-1/",
    }


def uncertainty_budget(data: UncertaintyBudgetInput) -> dict:
    sources = [item.source for item in data.components]
    uncertainties = []
    component_rows = []
    residual_vectors: dict[str, list[float]] = {}
    residual_ids: dict[str, dict[str, float]] = {}
    for item in data.components:
        if item.standard_uncertainty_tco2e is not None:
            standard_uncertainty = item.standard_uncertainty_tco2e
            basis = "user-entered validated standard uncertainty"
            n = None
            residual_mean = None
        else:
            sample = item.observations_tco2e
            standard_uncertainty = stdev(sample)
            residual_mean = mean(sample)
            residual_vectors[item.source] = [value - residual_mean for value in sample]
            if item.observation_ids:
                residual_ids[item.source] = dict(zip(item.observation_ids, sample))
            basis = "empirical centered validation residuals; bootstrap distribution"
            n = len(sample)
        uncertainties.append(standard_uncertainty)
        component_rows.append({"source": item.source, "standard_uncertainty_tco2e": standard_uncertainty,
                               "basis": basis, "observation_count": n,
                               "validation_residual_mean_tco2e": residual_mean, "evidence": item.evidence})

    correlation = [[1.0 if i == j else 0.0 for j in range(len(sources))] for i in range(len(sources))]
    supplied_correlations = {tuple(sorted((pair.source_a, pair.source_b))): pair.correlation for pair in data.correlations}
    derived_pairs = []
    for pair in data.correlations:
        i, j = sources.index(pair.source_a), sources.index(pair.source_b)
        correlation[i][j] = correlation[j][i] = pair.correlation
    for i, source_a in enumerate(sources):
        for j in range(i + 1, len(sources)):
            source_b = sources[j]
            shared_ids = set(residual_ids.get(source_a, {})) & set(residual_ids.get(source_b, {}))
            if len(shared_ids) < 2:
                continue
            if tuple(sorted((source_a, source_b))) in supplied_correlations:
                raise ValueError("Korelasi pasangan dengan ID observasi bersama dihitung dari residual; hapus korelasi manual pasangan tersebut.")
            left = [residual_ids[source_a][key] for key in sorted(shared_ids)]
            right = [residual_ids[source_b][key] for key in sorted(shared_ids)]
            left_mean, right_mean = mean(left), mean(right)
            denom = math.sqrt(sum((x - left_mean) ** 2 for x in left) * sum((y - right_mean) ** 2 for y in right))
            estimate = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right)) / denom if denom else 0.0
            correlation[i][j] = correlation[j][i] = max(-1.0, min(1.0, estimate))
            derived_pairs.append({"source_a": source_a, "source_b": source_b, "correlation": correlation[i][j], "paired_observation_count": len(shared_ids)})
    covariance = [[uncertainties[i] * correlation[i][j] * uncertainties[j]
                   for j in range(len(sources))] for i in range(len(sources))]
    # Cholesky factorization validates that the user supplied correlations form
    # a positive-semidefinite covariance matrix and supports correlated draws.
    lower = [[0.0 for _ in sources] for _ in sources]
    for i in range(len(sources)):
        for j in range(i + 1):
            residual = correlation[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if residual < -1e-8:
                    raise ValueError("Matriks korelasi tidak positive-semidefinite; periksa korelasi antar sumber.")
                lower[i][j] = math.sqrt(max(0.0, residual))
            elif lower[j][j] > 1e-12:
                lower[i][j] = residual / lower[j][j]
            elif abs(residual) > 1e-8:
                raise ValueError("Matriks korelasi singular dan tidak konsisten.")
    variance = sum(sum(row) for row in covariance)
    if variance < -1e-8:
        raise ValueError("Varians gabungan negatif; korelasi tidak konsisten dengan ketidakpastian.")
    combined = math.sqrt(max(0.0, variance))
    z = NormalDist().inv_cdf(0.5 + data.confidence_level_pct / 200)
    rng = random.Random(data.seed)
    draws = []
    for _ in range(data.simulation_draws):
        independent = [rng.gauss(0.0, 1.0) for _ in sources]
        correlated = [sum(lower[i][j] * independent[j] for j in range(i + 1)) for i in range(len(sources))]
        component_draws = []
        for i, source in enumerate(sources):
            residuals = residual_vectors.get(source)
            if residuals:
                percentile = NormalDist().cdf(correlated[i])
                ordered = sorted(residuals)
                residual = ordered[min(len(ordered) - 1, int(percentile * len(ordered)))]
                component_draws.append(residual)
            else:
                component_draws.append(uncertainties[i] * correlated[i])
        draws.append(data.central_net_tco2e + sum(component_draws))
    draws.sort()
    alpha = (1.0 - data.confidence_level_pct / 100) / 2
    lower_bound = draws[max(0, min(len(draws) - 1, int(alpha * len(draws))))]
    upper_bound = draws[max(0, min(len(draws) - 1, int((1 - alpha) * len(draws)) - 1))]
    denominator = variance or 1.0
    ranked = []
    for i in range(len(data.components)):
        contribution = sum(covariance[i])
        ranked.append({**component_rows[i], "correlated_variance_contribution_tco2e2": contribution,
                       "variance_share_pct": contribution / denominator * 100 if variance else 0.0})
    ranked.sort(key=lambda row: row["variance_share_pct"], reverse=True)
    covered_correlation_pairs = set(supplied_correlations) | {
        tuple(sorted((pair["source_a"], pair["source_b"]))) for pair in derived_pairs
    }
    total_correlation_pairs = len(data.components) * (len(data.components) - 1) // 2
    return {
        "algorithm": "correlated_uncertainty_monte_carlo_v2",
        "central_net_tco2e": data.central_net_tco2e,
        "combined_standard_uncertainty_tco2e": combined,
        "confidence_level_pct": data.confidence_level_pct,
        "normal_quantile": z,
        "interval_tco2e": [lower_bound, upper_bound],
        "conservative_lower_bound_tco2e": lower_bound,
        "simulated_interval_method": "correlated Gaussian copula with empirical centered validation-residual marginals when residual samples are supplied; otherwise normal standard-uncertainty marginals",
        "simulation_draws": data.simulation_draws,
        "random_seed": data.seed,
        "covariance_matrix_tco2e2": covariance,
        "correlation_matrix": correlation,
        "correlations_derived_from_shared_validation_ids": derived_pairs,
        "component_residual_biases_tco2e": {row["source"]: row["validation_residual_mean_tco2e"] for row in component_rows if row["validation_residual_mean_tco2e"] is not None},
        "largest_uncertainty_source": ranked[0]["source"],
        "sources_ranked": ranked,
        "assumption": data.correlation_note,
        "warnings": (["unreported_pairwise_correlations_assumed_zero"] if len(covered_correlation_pairs) < total_correlation_pairs else []),
        "qualification": "Monte Carlo uses empirical centered residual distributions when residual samples exist and derives pairwise correlation from shared validation IDs. Other marginal distributions use a normal model. Unpaired and unentered correlations remain zero by assumption; source uncertainty evidence is still required and this is not the methodology uncertainty deduction.",
    }


def vmd0054_leakage(data: VMD0054LeakageInput) -> dict:
    """Calculate the entered-data production-to-leakage pathway in VMD0054 v1.1."""
    rows = []
    total_land_impact_ha = 0.0
    for item in data.commodities:
        growth = 1.0 + item.annual_yield_growth_pct / 100.0
        baseline_production = (
            0.0 if item.introduced_by_project
            else mean(item.historical_project_production) * growth ** data.monitoring_year
        )
        project_change = baseline_production - item.monitored_project_production
        mitigation_baseline = 0.0
        mitigation_increase = 0.0
        if item.mitigation_historical_production:
            mitigation_baseline = mean(item.mitigation_historical_production) * growth ** data.monitoring_year
            mitigation_increase = (item.mitigation_monitored_production or 0.0) - mitigation_baseline
        elif item.mitigation_fuelwood_new_plantation:
            mitigation_increase = item.mitigation_monitored_production or 0.0
        net_change = project_change - mitigation_increase
        if data.cross_commodity_accounting:
            default_supply_pct, default_new_land_pct = 100.0, 100.0
        elif item.commodity_type == "fuelwood":
            default_supply_pct, default_new_land_pct = 100.0, 100.0
        else:
            default_supply_pct, default_new_land_pct = 75.0, 40.0
        increased_supply_pct = item.increased_supply_pct if item.increased_supply_pct is not None else default_supply_pct
        new_land_pct = item.new_land_pct if item.new_land_pct is not None else default_new_land_pct
        land_impact = (
            net_change * increased_supply_pct / 100.0
            * new_land_pct / 100.0 / item.yield_on_new_land_units_per_ha
        )
        total_land_impact_ha += land_impact
        rows.append({
            "commodity": item.commodity, "unit": item.unit,
            "historical_reference_years": len(item.historical_project_production),
            "baseline_production": baseline_production,
            "monitored_project_production": item.monitored_project_production,
            "project_production_change": project_change,
            "mitigation_baseline_production": mitigation_baseline,
            "mitigation_production_increase": mitigation_increase,
            "net_change_subject_to_leakage": net_change,
            "commodity_type": item.commodity_type,
            "introduced_by_project": item.introduced_by_project,
            "increased_supply_pct": increased_supply_pct,
            "increased_supply_default_pct": default_supply_pct,
            "new_land_pct": new_land_pct,
            "new_land_default_pct": default_new_land_pct,
            "yield_on_new_land_units_per_ha": item.yield_on_new_land_units_per_ha,
            "new_land_area_impact_ha": land_impact,
            "evidence": item.evidence,
        })
    leakage_area_ha = max(0.0, total_land_impact_ha)
    land_conversion_emissions = leakage_area_ha * data.carbon_stock_loss_t_c_ha * 44 / 12
    total_leakage = land_conversion_emissions + data.mitigation_emissions_tco2e_cumulative
    warnings = ["verify_regional_land_carbon_stock_loss_and_land_yield_sources"]
    if data.monitoring_year < 5:
        warnings.append("vmd0054_leakage_monitoring_minimum_five_years_not_yet_complete")
    if data.cross_commodity_accounting:
        warnings.append("cross_commodity_national_production_trend_and_default_factors_require_evidence")
    return {
        "algorithm": "vmd0054_v1_1_entered_data_equations_1_6_9_13_screening",
        "methodology": {"id": data.methodology_id, "version": data.methodology_version,
                        "module_id": "VMD0054", "module_version": data.vmd0054_version},
        "project_start_date": data.project_start_date.isoformat(),
        "monitoring_year": data.monitoring_year,
        "cross_commodity_accounting": data.cross_commodity_accounting,
        "commodities": rows,
        "net_land_impact_before_floor_ha": total_land_impact_ha,
        "area_generating_leakage_ha": leakage_area_ha,
        "carbon_stock_loss_t_c_ha": data.carbon_stock_loss_t_c_ha,
        "land_conversion_leakage_tco2e": land_conversion_emissions,
        "leakage_mitigation_emissions_tco2e_cumulative": data.mitigation_emissions_tco2e_cumulative,
        "total_leakage_tco2e": total_leakage,
        "equation_basis": "VMD0054 v1.1 equations 1-6 and 9-13; entered production, yield, land factors and stock-loss data; negative aggregate land impact is floored at zero.",
        "evidence": data.evidence,
        "warnings": warnings + (["cross_commodity_default_factors_applied; verify national production trend and introduced commodity evidence"] if data.cross_commodity_accounting else []),
        "qualification": "Entered-data pathway only. SaveGeo does not establish historical reference periods, commodity classification, regional yield, IS/NL factor appropriateness, forest/ecoregion carbon loss, source hierarchy or full VMD0054 applicability. A VVB must review the inputs and result.",
        "source": "https://verra.org/methodologies/vmd0054-estimating-leakage-from-the-displacement-of-agricultural-activities-v1-1/",
    }


def blue_carbon_monitoring(data: BlueCarbonMonitoringInput) -> dict:
    rows = []
    for period in data.periods:
        biomass = period.biomass_mg_c_ha * period.habitat_area_ha
        sediment = period.sediment_soc_mg_c_ha * period.habitat_area_ha
        baseline_present = period.baseline_habitat_area_ha is not None
        baseline_biomass = ((period.baseline_biomass_mg_c_ha or 0) * (period.baseline_habitat_area_ha or 0)) if baseline_present else None
        baseline_sediment = ((period.baseline_sediment_soc_mg_c_ha or 0) * (period.baseline_habitat_area_ha or 0)) if baseline_present else None
        baseline_inventory = ((baseline_biomass or 0) + (baseline_sediment or 0)) * 44 / 12 if baseline_present else None
        inventory = (biomass + sediment) * 44 / 12
        previous_erosion_area = rows[-1]["erosion_area_ha"] if rows else 0.0
        erosion_increment = (
            max(0.0, period.erosion_area_ha - previous_erosion_area)
            if period.erosion_area_basis == "cumulative" else period.erosion_area_ha
        )
        erosion_emissions = None
        if period.erosion_driver == "other" and period.eroded_soil_carbon_t_c_ha is not None:
            erosion_emissions = (erosion_increment * period.eroded_soil_carbon_t_c_ha
                                 * (period.eroded_carbon_oxidized_pct or 0) / 100 * 44 / 12)
        direct_se_available = all(value is not None for value in (
            period.habitat_area_se_ha, period.biomass_se_mg_c_ha, period.sediment_soc_se_mg_c_ha,
        ))
        if direct_se_available:
            density_variance = max(0.0, (period.biomass_se_mg_c_ha or 0) ** 2
                                   + (period.sediment_soc_se_mg_c_ha or 0) ** 2
                                   + 2 * period.biomass_soc_correlation
                                   * (period.biomass_se_mg_c_ha or 0) * (period.sediment_soc_se_mg_c_ha or 0))
            stock_se = math.sqrt(period.habitat_area_ha ** 2 * density_variance
                                 + (period.biomass_mg_c_ha + period.sediment_soc_mg_c_ha) ** 2
                                 * (period.habitat_area_se_ha or 0) ** 2) * 44 / 12
            uncertainty_basis = "propagated biomass, sediment SOC, and habitat-area SE with biomass/SOC covariance"
        elif data.uncertainty_pct is not None:
            stock_se = abs((biomass + sediment) * 44 / 12) * data.uncertainty_pct / 100
            uncertainty_basis = "entered relative inventory standard uncertainty"
        else:
            stock_se = None
            uncertainty_basis = "missing"
        rows.append({
            "year": period.year,
            "habitat": period.habitat,
            "habitat_area_ha": period.habitat_area_ha,
            "biomass_stock_mg_c": biomass,
            "sediment_soc_stock_mg_c": sediment,
            "total_inventory_tco2e": inventory,
            "baseline_inventory_tco2e": baseline_inventory,
            "project_minus_baseline_inventory_tco2e": inventory - baseline_inventory if baseline_inventory is not None else None,
            "inventory_standard_uncertainty_tco2e": stock_se,
            "uncertainty_basis": uncertainty_basis,
            "erosion_area_ha": period.erosion_area_ha,
            "erosion_area_basis": period.erosion_area_basis,
            "erosion_increment_area_ha": erosion_increment,
            "erosion_driver": period.erosion_driver,
            "eroded_soil_carbon_t_c_ha": period.eroded_soil_carbon_t_c_ha,
            "eroded_carbon_oxidized_pct": period.eroded_carbon_oxidized_pct,
            "estimated_non_slr_erosion_emissions_tco2e": erosion_emissions,
            "submergence_area_ha": period.submergence_area_ha,
            "relative_sea_level_rise_mm_yr": period.relative_sea_level_rise_mm_yr,
            "ch4_emissions_tco2e_yr": period.ch4_emissions_tco2e_yr,
            "n2o_emissions_tco2e_yr": period.n2o_emissions_tco2e_yr,
            "baseline_ch4_emissions_tco2e_yr": period.baseline_ch4_emissions_tco2e_yr,
            "baseline_n2o_emissions_tco2e_yr": period.baseline_n2o_emissions_tco2e_yr,
            "sea_level_risk_buffer_contribution_tco2e": period.sea_level_risk_buffer_contribution_tco2e,
            "source": period.source,
        })
    first, last = rows[0], rows[-1]
    warnings = []
    if last["habitat_area_ha"] < first["habitat_area_ha"]:
        warnings.append("habitat_area_declined_between_observations")
    if last["erosion_area_ha"] > first["erosion_area_ha"]:
        warnings.append("reported_erosion_area_increased")
    if last["relative_sea_level_rise_mm_yr"] and last["relative_sea_level_rise_mm_yr"] > 0:
        warnings.append("relative_sea_level_rise_reported")
    if last["submergence_area_ha"] > first["submergence_area_ha"]:
        warnings.append("reported_submergence_area_increased")
    delta = last["total_inventory_tco2e"] - first["total_inventory_tco2e"]
    elapsed_years = last["year"] - first["year"]
    ghg_inputs_complete = all(row["ch4_emissions_tco2e_yr"] is not None and row["n2o_emissions_tco2e_yr"] is not None for row in rows)
    ghg_emissions = None
    net_delta = None
    if ghg_inputs_complete:
        # Trapezoidal integration over the measured interval, using only the
        # project-side rates supplied by the user.
        ghg_emissions = sum(
            (((rows[index - 1]["ch4_emissions_tco2e_yr"] or 0) + (rows[index - 1]["n2o_emissions_tco2e_yr"] or 0)
              + (rows[index]["ch4_emissions_tco2e_yr"] or 0) + (rows[index]["n2o_emissions_tco2e_yr"] or 0)) / 2)
            * (rows[index]["year"] - rows[index - 1]["year"])
            for index in range(1, len(rows))
        )
        net_delta = delta - ghg_emissions
    else:
        warnings.append("ch4_or_n2o_emission_inputs_missing_net_balance_not_reported")
    baseline_complete = all(row["project_minus_baseline_inventory_tco2e"] is not None for row in rows)
    additional_stock_change = (
        last["project_minus_baseline_inventory_tco2e"] - first["project_minus_baseline_inventory_tco2e"]
        if baseline_complete else None
    )
    additional_ghg = None
    net_additional = None
    if baseline_complete and all(
        row["ch4_emissions_tco2e_yr"] is not None and row["n2o_emissions_tco2e_yr"] is not None
        and row["baseline_ch4_emissions_tco2e_yr"] is not None and row["baseline_n2o_emissions_tco2e_yr"] is not None
        for row in rows
    ):
        additional_ghg = sum(
            (((rows[index - 1]["ch4_emissions_tco2e_yr"] or 0) + (rows[index - 1]["n2o_emissions_tco2e_yr"] or 0)
              - (rows[index - 1]["baseline_ch4_emissions_tco2e_yr"] or 0) - (rows[index - 1]["baseline_n2o_emissions_tco2e_yr"] or 0)
              + (rows[index]["ch4_emissions_tco2e_yr"] or 0) + (rows[index]["n2o_emissions_tco2e_yr"] or 0)
              - (rows[index]["baseline_ch4_emissions_tco2e_yr"] or 0) - (rows[index]["baseline_n2o_emissions_tco2e_yr"] or 0)) / 2)
            * (rows[index]["year"] - rows[index - 1]["year"])
            for index in range(1, len(rows))
        )
        net_additional = additional_stock_change - additional_ghg
    else:
        warnings.append("baseline_relative_net_change_requires_project_and_baseline_ch4_n2o_for_every_period")
    erosion_emissions_total = sum(row["estimated_non_slr_erosion_emissions_tco2e"] or 0 for row in rows[1:])
    if not baseline_complete:
        warnings.append("baseline_project_difference_not_calculated_baseline_inputs_missing")
    if erosion_emissions_total > 0:
        warnings.append("non_slr_erosion_emissions_are_screened_separately_baseline_erosion_is_not_input")
        net_additional = None
    slr_buffer = last["sea_level_risk_buffer_contribution_tco2e"]
    if slr_buffer is not None:
        warnings.append("sea_level_risk_buffer_is_reported_separately_and_not_deducted_to_avoid_double_counting")
    warnings.append("vm0033_soc_100_year_depletion_and_depth_schedule_not_modelled")
    warnings.append("submergence_area_is_reported_as_a_risk_indicator_not_directly_converted_to_emissions")
    change_uncertainty = (
        math.sqrt(max(0.0, first["inventory_standard_uncertainty_tco2e"] ** 2 + last["inventory_standard_uncertainty_tco2e"] ** 2 -
                      2 * data.temporal_correlation * first["inventory_standard_uncertainty_tco2e"] * last["inventory_standard_uncertainty_tco2e"]))
        if first["inventory_standard_uncertainty_tco2e"] is not None and last["inventory_standard_uncertainty_tco2e"] is not None
        else None
    )
    change_half_width_90 = 1.645 * change_uncertainty if change_uncertainty is not None else None
    if change_uncertainty is None:
        warnings.append("blue_carbon_uncertainty_not_propagated_missing_input")
    return {
        "algorithm": "blue_carbon_inventory_baseline_ghg_erosion_risk_screening_v3",
        "habitat": first["habitat"],
        "periods": rows,
        "inventory_change_tco2e": delta,
        "annualized_inventory_change_tco2e_yr": delta / elapsed_years,
        "baseline_relative_additional_stock_change_tco2e": additional_stock_change,
        "baseline_relative_additional_stock_change_tco2e_yr": additional_stock_change / elapsed_years if additional_stock_change is not None else None,
        "baseline_relative_additional_ch4_n2o_tco2e": additional_ghg,
        "baseline_relative_net_change_tco2e": net_additional,
        "baseline_relative_net_change_tco2e_yr": net_additional / elapsed_years if net_additional is not None else None,
        "estimated_non_slr_erosion_emissions_tco2e_screening": erosion_emissions_total,
        "sea_level_risk_buffer_contribution_tco2e": slr_buffer,
        "reported_ch4_n2o_emissions_tco2e_over_period": ghg_emissions,
        "net_stock_change_after_reported_ch4_n2o_tco2e": net_delta,
        "annualized_net_change_after_reported_ch4_n2o_tco2e_yr": net_delta / elapsed_years if net_delta is not None else None,
        "uncertainty_pct": data.uncertainty_pct,
        "temporal_correlation": data.temporal_correlation,
        "inventory_change_standard_uncertainty_tco2e": change_uncertainty,
        "inventory_change_interval_90_tco2e": [delta - change_half_width_90, delta + change_half_width_90] if change_half_width_90 is not None else None,
        "warnings": warnings,
        "qualification": "Inventory change uses entered habitat area and biomass/SOC densities. When complete baseline series are supplied, the output includes change in project-minus-baseline inventory and trapezoidally integrated project-minus-baseline CH4/N2O. Non-sea-level erosion is estimated from entered eroded soil carbon, area and oxidized fraction and is withheld from net additionality because baseline erosion is not supplied. Sea-level-rise buffer contribution is reported separately and not subtracted. Submergence is a risk indicator. Uncertainty propagation and the 100-year SOC/depth schedule remain simplified; this is not a full VM0033/VMD0051 calculator and VVB review remains required.",
        "methodology_sources": [
            "https://verra.org/methodologies/vm0033-methodology-for-tidal-wetland-and-seagrass-restoration-v2-1/",
            "https://verra.org/methodologies/methods-for-monitoring-of-carbon-stock-changes-and-greenhouse-gas-emissions-and-removals-in-tidal-wetland-restoration-and-conservation-project-activities-m-tw-v1-0/",
        ],
    }


def _sample_points(stratum, count: int, seed: int) -> list[dict] | None:
    if not stratum.geometry or count <= 0:
        return None
    from pyproj import CRS, Transformer
    from shapely.geometry import Point, shape
    from shapely.ops import transform

    geom = shape(stratum.geometry)
    if geom.is_empty or not geom.is_valid or geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"Geometri strata '{stratum.name}' harus Polygon/MultiPolygon GeoJSON yang valid.")
    center = geom.representative_point()
    local = CRS.from_proj4(f"+proj=laea +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m +no_defs")
    forward = Transformer.from_crs("EPSG:4326", local, always_xy=True).transform
    inverse = Transformer.from_crs(local, "EPSG:4326", always_xy=True).transform
    projected = transform(forward, geom)
    minx, miny, maxx, maxy = projected.bounds
    rng = random.Random(seed)
    points = []
    attempts = max(10000, count * 2000)
    for _ in range(attempts):
        if len(points) >= count:
            break
        candidate = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if projected.covers(candidate):
            lon, lat = inverse(candidate.x, candidate.y)
            points.append({"lon": round(lon, 7), "lat": round(lat, 7)})
    if len(points) != count:
        raise ValueError(f"Tidak cukup titik sampel di strata '{stratum.name}'. Periksa geometri.")
    return points


def sampling_plan(data: SamplingPlanInput) -> dict:
    z = NormalDist().inv_cdf(0.5 + data.confidence_level_pct / 200)
    total_area = sum(s.area_ha for s in data.strata)
    allocations = []
    total_n = 0
    for index, stratum in enumerate(data.strata):
        n = math.ceil((z * stratum.expected_cv_pct / data.target_precision_pct) ** 2)
        total_n += n
        allocations.append({"stratum": stratum.name, "area_ha": stratum.area_ha,
                            "area_weight_pct": stratum.area_ha / total_area * 100,
                            "expected_cv_pct": stratum.expected_cv_pct,
                            "recommended_plots": n,
                            "plot_locations": _sample_points(stratum, n, data.seed + index) if n <= 500 else None})
    warnings = []
    if total_n > 500:
        warnings.append("recommended_sample_exceeds_500_locations; split field campaigns")
    if any(row["plot_locations"] is None for row in allocations):
        warnings.append("upload_valid_stratum_polygons_to_generate_random_plot_coordinates")
    return {
        "algorithm": "stratified_coefficient_of_variation_sample_size_v1",
        "confidence_level_pct": data.confidence_level_pct,
        "target_precision_pct": data.target_precision_pct,
        "z_value": z,
        "recommended_plot_count": total_n,
        "strata": allocations,
        "pools": data.pools,
        "measurement_checklist": {pool: ["plot_code", "visit_date", "area_m2", "method_and_units", "observer", "QA/QC", "photo_or_field_sheet"] for pool in data.pools},
        "revisit_interval_months": data.revisit_interval_months,
        "next_visit_rule": f"Schedule repeat measurement {data.revisit_interval_months} months after the first completed visit.",
        "random_seed": data.seed,
        "warnings": warnings,
        "qualification": "Planning recommendation based on user-entered expected CV and target relative precision; it is not a methodology-prescribed sample size. Confirm stratum design, plot size, nonresponse and precision calculation with the approved field protocol.",
    }


def registry_reconciliation(
    data: RegistryReconciliationInput,
    internal_events: list[dict],
    internal_project_id: str | None = None,
) -> dict:
    internal = {(row.get("registry_name", "").casefold(), row.get("external_transaction_id", "")): row for row in internal_events}
    rows = []
    for external in data.rows:
        local = internal.get((data.registry.casefold(), external.transaction_id))
        rows.append({
            **external.model_dump(mode="json"),
            "match_status": "matched" if local else "external_only",
            "internal_event_id": local.get("event_id") if local else None,
            "quantity_matches": bool(local and local.get("quantity") == external.quantity),
            "date_matches": bool(local and str(local.get("effective_date")) == external.event_date.isoformat()),
            "event_type_matches": bool(local and external.event_type == str(local.get("event_type", "")).removesuffix("_reported")),
            "serial_matches": bool(local and (external.serial == local.get("batch_reference") or
                                              str(local.get("batch_reference", "")) in external.serial)),
            "account_matches": bool(local and external.account in {local.get("owner_from"), local.get("owner_to")} ),
            "project_id_matches": external.external_project_id == internal_project_id if internal_project_id else None,
        })
    fields_to_compare = ("quantity_matches", "date_matches", "event_type_matches", "serial_matches", "account_matches")
    warnings = ["imported_registry_rows_are_not_live_verified; internal_ledger_was_not_changed"]
    if internal_project_id is None:
        warnings.append("internal_registry_project_reference_not_configured")
    return {
        "algorithm": "external_registry_import_reconciliation_v1",
        "registry": data.registry,
        "imported_at": data.imported_at.isoformat(),
        "source_url": str(data.source_url) if data.source_url else None,
        "source_file_sha256": data.source_file_sha256,
        "row_count": len(rows),
        "matched_count": sum(row["match_status"] == "matched" for row in rows),
        "mismatch_count": sum(row["match_status"] == "matched" and (
            not all(row[field] for field in fields_to_compare) or row["project_id_matches"] is False
        ) for row in rows),
        "external_only_count": sum(row["match_status"] == "external_only" for row in rows),
        "rows_preview": [row for row in rows if row["match_status"] == "external_only" or
                         not all(row[field] for field in fields_to_compare) or row["project_id_matches"] is False][:100],
        "rows": rows,
        "warnings": warnings,
        "qualification": "Reconciliation compares imported evidence to SaveGeo's reported ledger. It does not connect to or certify the external registry and does not alter internal events.",
    }


def safeguard_indicator(data: SafeguardIndicatorInput) -> dict:
    progress = None if data.target in (None, 0) else data.value / data.target * 100
    return {
        "period": data.period, "indicator": data.indicator, "value": data.value,
        "unit": data.unit, "target": data.target, "progress_pct": progress,
        "target_met": data.value >= data.target if data.target is not None else None,
        "methodology": data.methodology, "owner": data.owner,
        "qualification": "Project-reported safeguard indicator; evidence and reviewer acceptance are required before external reporting.",
    }


def community_benefit(data: CommunityBenefitInput) -> dict:
    return {
        "period": data.period, "community_or_group": data.community_or_group,
        "benefit_type": data.benefit_type, "value": data.value,
        "currency_or_unit": data.currency_or_unit, "status": data.status,
        "consent_reference": data.consent_reference,
        "delivery_reference": data.delivery_reference,
        "recipient_acknowledged": data.recipient_acknowledged,
        "beneficiary_count": data.beneficiary_count,
        "verified_for_reporting": False,
        "reviewer_note": data.reviewer_note,
        "qualification": "Benefit-sharing record is project reported. It is not proof of payment, consent, or community agreement without accepted evidence.",
    }
