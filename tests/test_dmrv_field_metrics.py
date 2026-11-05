from decimal import Decimal

import pytest

from app.services.dmrv.field_metrics import DEFAULT_CARBON_FRACTION, plot_density

BU_DESSY_T1_KG = [205.4233029, 137.8348107, 382.8003569, 24.91593574]


def test_density_uses_area_and_carbon_fraction():
    result = plot_density(BU_DESSY_T1_KG, 400.0, "assumed")
    expected_biomass = Decimal(str(sum(BU_DESSY_T1_KG))) / Decimal(1000) / (Decimal(400) / Decimal(10000))
    assert result.source_type == "field_measurement"
    assert result.biomass_dry_mg_per_ha == pytest.approx(expected_biomass, rel=Decimal("1e-9"))
    assert result.carbon_mg_c_per_ha == pytest.approx(expected_biomass * Decimal("0.47"), rel=Decimal("1e-9"))
    assert float(result.biomass_dry_mg_per_ha) == pytest.approx(18.7752, abs=1e-3)
    assert (result.tree_count, result.trees_without_biomass, result.blockers) == (4, 0, ())
    assert result.area_basis == "assumed" and result.carbon_fraction == DEFAULT_CARBON_FRACTION


def test_unknown_area_is_a_blocker_not_a_guess():
    result = plot_density(BU_DESSY_T1_KG, None, None)
    assert result.biomass_dry_mg_per_ha is None and result.carbon_mg_c_per_ha is None
    assert result.blockers == ("plot_area_unknown",)


def test_no_biomass_values_is_a_blocker():
    result = plot_density([None, None], 400.0, "measured")
    assert result.carbon_mg_c_per_ha is None
    assert result.blockers == ("no_biomass_values",)
    assert (result.tree_count, result.trees_without_biomass) == (2, 2)


def test_zero_kg_tree_counts_as_a_value():
    result = plot_density([0.0, None], 100.0, "measured")
    assert result.carbon_mg_c_per_ha == Decimal(0)
    assert result.trees_without_biomass == 1 and result.blockers == ()


@pytest.mark.parametrize("area", [0.0, -5.0])
def test_non_positive_area_is_rejected(area):
    with pytest.raises(ValueError, match="area_m2"):
        plot_density([1.0], area, "measured")
