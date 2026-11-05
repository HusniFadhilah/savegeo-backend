from decimal import Decimal

import pytest

from app.services.dmrv.accounting import (
    ArrIllustrativeInputs,
    calculate_arr_illustrative_ledger,
)


def fixture_inputs(**overrides):
    values = {
        "area_ha": Decimal("100"),
        "project_stock_start_mg_c_per_ha": Decimal("100"),
        "project_stock_end_mg_c_per_ha": Decimal("120"),
        "baseline_stock_start_mg_c_per_ha": Decimal("100"),
        "baseline_stock_end_mg_c_per_ha": Decimal("105"),
    }
    values.update(overrides)
    return ArrIllustrativeInputs(**values)


def test_arr_golden_fixture_is_labeled_and_preserves_missing_components():
    result = calculate_arr_illustrative_ledger(fixture_inputs())

    assert result.line("project_stock_change").value == Decimal("2000")
    assert result.line("baseline_stock_change").value == Decimal("500")
    assert result.line("additional_stock_change").value == Decimal("1500")
    assert result.line("gross_additional_removal").value == Decimal("5500")
    assert result.line("net_mitigation").value is None
    assert result.line("project_emissions").status == "requires_data"
    assert result.line("leakage").status == "requires_data"
    assert result.blockers == ("project_emissions_tco2e", "leakage_tco2e")
    assert result.qualification == "illustrative_only"
    assert result.methodology_profile is None
    assert result.verification_status == "not_verified"
    assert result.issuance_status == "not_issued"


def test_known_zero_emissions_are_not_treated_as_missing():
    result = calculate_arr_illustrative_ledger(
        fixture_inputs(project_emissions_tco2e=0, leakage_tco2e="0")
    )

    assert result.line("project_emissions").value == Decimal("0")
    assert result.line("leakage").value == Decimal("0")
    assert result.line("net_mitigation").value == Decimal("5500")
    assert result.blockers == ()


def test_negative_mitigation_is_retained():
    result = calculate_arr_illustrative_ledger(
        fixture_inputs(
            project_stock_end_mg_c_per_ha=Decimal("90"),
            baseline_stock_end_mg_c_per_ha=Decimal("105"),
            project_emissions_tco2e=0,
            leakage_tco2e=0,
        )
    )

    assert result.line("additional_stock_change").value == Decimal("-1500")
    assert result.line("gross_additional_removal").value == Decimal("-5500")
    assert result.line("net_mitigation").value == Decimal("-5500")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("area_ha", 0, "greater than zero"),
        ("project_stock_end_mg_c_per_ha", -1, "must not be negative"),
        ("leakage_tco2e", -1, "must not be negative"),
        ("project_emissions_tco2e", float("nan"), "finite decimal"),
    ],
)
def test_invalid_values_are_rejected(field, value, message):
    with pytest.raises(ValueError, match=message):
        calculate_arr_illustrative_ledger(fixture_inputs(**{field: value}))