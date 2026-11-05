"""Plot-level biomass and carbon density from reported tree biomass.

Densities are returned only together with their area basis. A plot whose area
is unknown yields a blocker, never an estimate. The carbon fraction is a
placeholder default (IPCC 2006 tropical woody biomass); an approved
methodology profile must supply the value used for any claim.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

DEFAULT_CARBON_FRACTION = Decimal("0.47")
_KG_PER_MG = Decimal(1000)
_M2_PER_HA = Decimal(10_000)


@dataclass(frozen=True)
class PlotDensity:
    source_type: str  # "field_measurement"
    biomass_dry_mg_per_ha: Decimal | None
    carbon_mg_c_per_ha: Decimal | None
    tree_count: int
    trees_without_biomass: int
    area_m2: Decimal | None
    area_basis: str | None
    carbon_fraction: Decimal
    blockers: tuple[str, ...]


def plot_density(
    biomass_kg: Sequence[float | None],
    area_m2: float | None,
    area_basis: str | None,
    *,
    carbon_fraction: Decimal = DEFAULT_CARBON_FRACTION,
) -> PlotDensity:
    if area_m2 is not None and area_m2 <= 0:
        raise ValueError("area_m2 must be greater than zero")
    values = [Decimal(str(value)) for value in biomass_kg if value is not None]
    missing = len(biomass_kg) - len(values)
    area = Decimal(str(area_m2)) if area_m2 is not None else None

    blockers: list[str] = []
    if area is None:
        blockers.append("plot_area_unknown")
    if not values:
        blockers.append("no_biomass_values")

    biomass_density = carbon_density = None
    if not blockers:
        biomass_density = sum(values, Decimal(0)) / _KG_PER_MG / (area / _M2_PER_HA)
        carbon_density = biomass_density * carbon_fraction
    return PlotDensity(
        source_type="field_measurement",
        biomass_dry_mg_per_ha=biomass_density,
        carbon_mg_c_per_ha=carbon_density,
        tree_count=len(biomass_kg),
        trees_without_biomass=missing,
        area_m2=area,
        area_basis=area_basis,
        carbon_fraction=carbon_fraction,
        blockers=tuple(blockers),
    )
