"""Regression check for the approved PT Dahana calibration reference file."""
from collections import defaultdict
from pathlib import Path

import pytest

from app.services.dmrv.field_import import ImportOptions, read_table, suggest_mapping, validate_rows
from app.services.dmrv.field_metrics import DEFAULT_CARBON_FRACTION, plot_density


DATASET = Path(__file__).parents[1] / "examples" / "dmrv" / "pt_dahana_subang_field_inventory.csv"


def test_approved_dahana_calibration_reference_has_ten_complete_measured_area_plots():
    headers, rows = read_table(DATASET.name, DATASET.read_bytes())
    report = validate_rows(rows, suggest_mapping(headers), ImportOptions(height_unit="m"))

    assert report.rows_total == 90
    assert report.error_rows == set()
    assert len(report.trees) == 90

    plots = defaultdict(list)
    for tree in report.trees:
        plots[tree.plot_code].append(tree)
    assert set(plots) == {str(plot) for plot in range(1, 11)}

    densities = []
    for trees in plots.values():
        assert {tree.area_m2 for tree in trees} == {400.0}
        assert {tree.area_basis for tree in trees} == {"measured"}
        result = plot_density(
            [tree.biomass_kg for tree in trees], 400, "measured",
            carbon_fraction=DEFAULT_CARBON_FRACTION,
        )
        assert result.biomass_dry_mg_per_ha is not None
        assert result.carbon_mg_c_per_ha == pytest.approx(
            result.biomass_dry_mg_per_ha * DEFAULT_CARBON_FRACTION
        )
        densities.append(result.carbon_mg_c_per_ha)

    assert len(densities) == 10
    assert all(value > 0 for value in densities)
