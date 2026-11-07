"""Illustrative-only carbon accounting primitives for ARR pilot fixtures.

This module does not implement or claim compliance with VM0047 or another
crediting methodology. Production calculations require an approved methodology
profile and complete, reviewed inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import TypeAlias

DecimalInput: TypeAlias = Decimal | int | float | str

_DECIMAL_PRECISION = 34
_ROUNDING_POLICY = "Decimal precision 34, ROUND_HALF_EVEN; no additional output quantization"
_CO2_NUMERATOR = Decimal("44")
_CO2_DENOMINATOR = Decimal("12")


@dataclass(frozen=True, slots=True)
class ArrIllustrativeInputs:
    """Carbon-stock densities and area for a pedagogical fixed-area example."""

    area_ha: DecimalInput
    project_stock_start_mg_c_per_ha: DecimalInput
    project_stock_end_mg_c_per_ha: DecimalInput
    baseline_stock_start_mg_c_per_ha: DecimalInput
    baseline_stock_end_mg_c_per_ha: DecimalInput
    project_emissions_tco2e: DecimalInput | None = None
    leakage_tco2e: DecimalInput | None = None


@dataclass(frozen=True, slots=True)
class LedgerLine:
    key: str
    value: Decimal | None
    unit: str
    status: str
    equation: str


@dataclass(frozen=True, slots=True)
class ArrIllustrativeLedger:
    source_type: str
    qualification: str
    formula_id: str
    methodology_profile: None
    verification_status: str
    issuance_status: str
    rounding_policy: str
    area_ha: Decimal
    normalized_inputs: tuple[tuple[str, Decimal | None], ...]
    lines: tuple[LedgerLine, ...]
    blockers: tuple[str, ...]

    def line(self, key: str) -> LedgerLine:
        """Return a named ledger line or raise KeyError if it does not exist."""
        for item in self.lines:
            if item.key == key:
                return item
        raise KeyError(key)


def _to_decimal(name: str, value: DecimalInput) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal number")
    return result


def _non_negative(name: str, value: DecimalInput) -> Decimal:
    result = _to_decimal(name, value)
    if result < 0:
        raise ValueError(f"{name} must not be negative")
    return result


def calculate_arr_illustrative_ledger(inputs: ArrIllustrativeInputs) -> ArrIllustrativeLedger:
    """Calculate an explicitly illustrative ARR fixed-area stock-change ledger.

    Missing project-emissions or leakage data stays missing and blocks net
    mitigation. A known zero is recorded as a computed zero. Negative stock
    change and net mitigation are retained to represent losses/reversals.
    """
    area = _to_decimal("area_ha", inputs.area_ha)
    if area <= 0:
        raise ValueError("area_ha must be greater than zero")

    project_start = _non_negative(
        "project_stock_start_mg_c_per_ha", inputs.project_stock_start_mg_c_per_ha
    )
    project_end = _non_negative(
        "project_stock_end_mg_c_per_ha", inputs.project_stock_end_mg_c_per_ha
    )
    baseline_start = _non_negative(
        "baseline_stock_start_mg_c_per_ha", inputs.baseline_stock_start_mg_c_per_ha
    )
    baseline_end = _non_negative(
        "baseline_stock_end_mg_c_per_ha", inputs.baseline_stock_end_mg_c_per_ha
    )
    project_emissions = (
        _non_negative("project_emissions_tco2e", inputs.project_emissions_tco2e)
        if inputs.project_emissions_tco2e is not None
        else None
    )
    leakage = (
        _non_negative("leakage_tco2e", inputs.leakage_tco2e)
        if inputs.leakage_tco2e is not None
        else None
    )

    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        project_change = (project_end - project_start) * area
        baseline_change = (baseline_end - baseline_start) * area
        additional_change = project_change - baseline_change
        gross_removal = additional_change * _CO2_NUMERATOR / _CO2_DENOMINATOR
        net_mitigation = (
            gross_removal - project_emissions - leakage
            if project_emissions is not None and leakage is not None
            else None
        )

    blockers = tuple(
        item
        for item, value in (
            ("project_emissions_tco2e", project_emissions),
            ("leakage_tco2e", leakage),
        )
        if value is None
    )
    lines = (
        LedgerLine(
            "project_stock_change",
            project_change,
            "Mg C",
            "computed",
            "(project_stock_end - project_stock_start) * area_ha",
        ),
        LedgerLine(
            "baseline_stock_change",
            baseline_change,
            "Mg C",
            "computed",
            "(baseline_stock_end - baseline_stock_start) * area_ha",
        ),
        LedgerLine(
            "additional_stock_change",
            additional_change,
            "Mg C",
            "computed",
            "project_stock_change - baseline_stock_change",
        ),
        LedgerLine(
            "gross_additional_removal",
            gross_removal,
            "tCO2",
            "computed",
            "additional_stock_change * 44 / 12",
        ),
        LedgerLine(
            "project_emissions",
            project_emissions,
            "tCO2e",
            "computed" if project_emissions is not None else "requires_data",
            "project emissions under the selected accounting boundary",
        ),
        LedgerLine(
            "leakage",
            leakage,
            "tCO2e",
            "computed" if leakage is not None else "requires_data",
            "leakage under the selected methodology",
        ),
        LedgerLine(
            "net_mitigation",
            net_mitigation,
            "tCO2e",
            "computed" if net_mitigation is not None else "requires_data",
            "gross_additional_removal - project_emissions - leakage",
        ),
    )
    return ArrIllustrativeLedger(
        source_type="accounting_result",
        qualification="illustrative_only",
        formula_id="arr_fixed_area_illustrative_v1",
        methodology_profile=None,
        verification_status="not_verified",
        issuance_status="not_issued",
        rounding_policy=_ROUNDING_POLICY,
        area_ha=area,
        normalized_inputs=(
            ("area_ha", area),
            ("project_stock_start_mg_c_per_ha", project_start),
            ("project_stock_end_mg_c_per_ha", project_end),
            ("baseline_stock_start_mg_c_per_ha", baseline_start),
            ("baseline_stock_end_mg_c_per_ha", baseline_end),
            ("project_emissions_tco2e", project_emissions),
            ("leakage_tco2e", leakage),
        ),
        lines=lines,
        blockers=blockers,
    )


def calculate_arr_illustrative_fixture() -> ArrIllustrativeLedger:
    """Return the fixed pedagogical ARR example without implying project data."""
    return calculate_arr_illustrative_ledger(
        ArrIllustrativeInputs(
            area_ha=Decimal("100"),
            project_stock_start_mg_c_per_ha=Decimal("100"),
            project_stock_end_mg_c_per_ha=Decimal("120"),
            baseline_stock_start_mg_c_per_ha=Decimal("100"),
            baseline_stock_end_mg_c_per_ha=Decimal("105"),
        )
    )
