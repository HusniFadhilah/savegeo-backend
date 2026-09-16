"""Read-only, formula-safe parser for the Bu Dessy carbon workbooks."""

from __future__ import annotations

import ast
import datetime as dt
import math
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.services.carbon_calibration_service import (
    CARBON_FRACTION,
    sha256_file,
)

_DMS_RE = re.compile(
    r"(?P<deg>\d{1,3})\s*(?:[\u00b0\u00ba\uFFFD]|deg)?\s*(?P<min>\d{1,2})\s*['\u2032\u2019]?\s*(?P<sec>\d{1,2}(?:[.,]\d+)?)\s*[\"\u2033\u201d]?\s*(?P<hem>[NSEW])",
    re.IGNORECASE,
)
_CELL_RE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_TRANSECT_RE = re.compile(
    r"no\.?\s*transek.*?(?P<id>T\s*\d+(?:\s*\([^)]*\))?(?:\s+tambahan)?)",
    re.IGNORECASE,
)
_FORMULA_ALLOWED_CHARS = re.compile(r"^[0-9eE+\-*/(). _a-zA-Z]+$")
_SPECIES_SYNONYMS = {
    "swietenia mahagoni": "Swietenia mahagoni",
    "swietenia macrophylla": "Swietenia macrophylla",
    "cinnamomum verum": "Cinnamomum verum",
    "pinus merkusii": "Pinus merkusii",
}


def _clean_text(value: Any) -> str:
    text = str(value or "")
    return " ".join(text.replace("\ufffd", "°").replace("\u00ef\u00bf\u00bd", "°").replace("\u00c2°", "°").replace("\u00c3\u201a", "").split())


def parse_decimal(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_text(value).replace(",", ".")
    if not text or text in {"-", "#REF!", "#VALUE!", "#N/A"}:
        return None
    match = _NUMBER_RE.fullmatch(text)
    return float(match.group(0)) if match else None


def parse_dms(value: Any) -> dict[str, Any]:
    raw = _clean_text(value)
    matches = list(_DMS_RE.finditer(raw.upper()))
    if len(matches) < 2:
        return {
            "raw": str(value) if value is not None else None,
            "latitude": None,
            "longitude": None,
            "status": "invalid" if raw else "missing",
        }
    result: dict[str, Any] = {"raw": str(value), "latitude": None, "longitude": None, "status": "valid"}
    for match in matches[:4]:
        degrees = float(match.group("deg"))
        minutes = float(match.group("min"))
        seconds = float(match.group("sec").replace(",", "."))
        decimal = degrees + minutes / 60 + seconds / 3600
        if match.group("hem") in {"S", "W"}:
            decimal = -decimal
        if match.group("hem") in {"N", "S"}:
            result["latitude"] = decimal
        else:
            result["longitude"] = decimal
    if result["latitude"] is None or result["longitude"] is None:
        result["status"] = "invalid"
    return result


def normalize_species(value: Any) -> dict[str, str | None]:
    raw = _clean_text(value)
    if not raw or raw in {"-", "#REF!", "#VALUE!"}:
        return {"raw": raw or None, "scientific_name": None, "local_name": None}
    parts = re.split(r"\s*/\s*", raw, maxsplit=1)
    scientific = parts[0].strip().lower()
    scientific_name = _SPECIES_SYNONYMS.get(
        scientific, " ".join(word.capitalize() for word in scientific.split())
    )
    return {
        "raw": raw,
        "scientific_name": scientific_name,
        "local_name": parts[1].strip() if len(parts) == 2 else None,
    }


class _SafeFormulaEvaluator(ast.NodeVisitor):
    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_Constant(self, node):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("Non-numeric formula constant")

    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            return left**right
        raise ValueError("Formula operator is not whitelisted")

    def visit_UnaryOp(self, node):
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        raise ValueError("Formula unary operator is not whitelisted")

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "LOG10" and len(node.args) == 1:
            return math.log10(self.visit(node.args[0]))
        raise ValueError("Formula function is not whitelisted")

    def generic_visit(self, node):
        raise ValueError(f"Formula token is not whitelisted: {type(node).__name__}")


def safe_evaluate_formula(formula: Any, cells: dict[str, Any]) -> float | None:
    return _safe_evaluate_formula(formula, cells, set())


def _safe_evaluate_formula(formula: Any, cells: dict[str, Any], seen: set[str]) -> float | None:
    if not isinstance(formula, str) or not formula.startswith("=") or "#REF!" in formula.upper():
        return None
    expression = formula[1:].replace("^", "**").replace("PI()", str(math.pi)).replace("LOG(", "LOG10(")
    if not _FORMULA_ALLOWED_CHARS.fullmatch(expression):
        return None

    def replace_cell(match: re.Match[str]) -> str:
        reference = match.group(0).upper()
        raw_value = cells.get(reference)
        value = parse_decimal(raw_value)
        if (
            value is None
            and isinstance(raw_value, str)
            and raw_value.startswith("=")
            and reference not in seen
        ):
            value = _safe_evaluate_formula(raw_value, cells, seen | {reference})
        if value is None:
            raise ValueError("Formula references a non-numeric cell")
        return str(value)

    try:
        expression = _CELL_RE.sub(replace_cell, expression)
        return float(_SafeFormulaEvaluator().visit(ast.parse(expression, mode="eval")))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _header_columns(values: list[Any]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, value in enumerate(values):
        text = _clean_text(value).lower()
        if "jenis pohon" in text:
            columns["species"] = index
        elif "keliling" in text:
            columns["circumference"] = index
        elif "diameter" in text:
            columns["dbh"] = index
        elif "tinggi" in text:
            columns["height"] = index
        elif "biomassa" in text:
            columns["biomass"] = index
        elif text in {"no", "no."}:
            columns["tree_id"] = index
        elif "status" in text or "kondisi" in text:
            columns["tree_status"] = index
        elif "sumber" in text or "reference" in text:
            columns["equation_source"] = index
    return columns


def _find_text(values: list[Any], needle: str) -> str | None:
    for value in values:
        text = _clean_text(value)
        if needle in text.lower():
            return text
    return None


def _carbon_fraction(rows: list[tuple[int, list[Any], list[Any]]]) -> float:
    labeled_fractions = []
    fallback_fractions = []
    for _, formulas, _ in rows:
        text = " ".join(str(value) for value in formulas if value is not None)
        for match in re.findall(r"\*\s*(0\.\d+)", text):
            fraction = float(match)
            if 0.3 <= fraction <= 0.7:
                (
                    labeled_fractions
                    if "karbon" in text.lower() or "carbon" in text.lower()
                    else fallback_fractions
                ).append(fraction)
    candidates = labeled_fractions or [
        value for value in fallback_fractions if math.isclose(value, CARBON_FRACTION)
    ]
    return Counter(candidates).most_common(1)[0][0] if candidates else CARBON_FRACTION


def parse_workbook(path: str | Path, dataset_id: str, campaign_year: int) -> dict[str, Any]:
    """Parse tree records and produce an audit without evaluating Excel formulas."""
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for workbook ingestion") from exc
    source = Path(path)
    digest, size = sha256_file(source)
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
    if any(name.startswith("xl/externalLinks/") for name in names):
        raise ValueError("External workbook links are not accepted")
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        raise ValueError("Macro-enabled workbook content is not accepted")
    formula_wb = openpyxl.load_workbook(source, read_only=True, data_only=False, keep_links=False)
    cached_wb = openpyxl.load_workbook(source, read_only=True, data_only=True, keep_links=False)
    records: list[dict[str, Any]] = []
    sheet_audits: list[dict[str, Any]] = []
    global_errors: list[dict[str, Any]] = []
    area_audit: list[dict[str, Any]] = []
    for sheet_name in formula_wb.sheetnames:
        formula_ws, cached_ws = formula_wb[sheet_name], cached_wb[sheet_name]
        rows = list(formula_ws.iter_rows(values_only=False))
        cached_rows = list(cached_ws.iter_rows(values_only=False))
        errors = []
        formula_count = 0
        for row in rows:
            for cell in row:
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    formula_count += 1
                if (isinstance(value, str) and "#REF!" in value.upper()) or cell.data_type == "e":
                    errors.append({"cell": cell.coordinate, "value": str(value), "kind": "broken_formula"})
        area_formula_cells = [
            {
                "cell": cell.coordinate,
                "formula": str(cell.value),
                "inferred_plot_area_m2": 400.0,
            }
            for row in rows
            for cell in row
            if isinstance(cell.value, str)
            and cell.value.startswith("=")
            and ("/400" in cell.value.replace(" ", "") or "400" in cell.value)
        ]
        if area_formula_cells:
            area_audit.append(
                {
                    "sheet": sheet_name,
                    "declared_or_inferred_plot_areas_m2": [400.0],
                    "formula_cells": area_formula_cells[:50],
                    "status": "review",
                    "warning": "400 m² is present in a workbook formula but is not accepted as confirmed plot geometry",
                }
            )
        if sheet_name.strip().lower() == "sheet3":
            global_errors.append(
                {
                    "sheet": sheet_name,
                    "kind": "excluded_sheet",
                    "reason": "Sheet3 contains broken references and is never a training source",
                }
            )
            sheet_audits.append(
                {
                    "sheet": sheet_name,
                    "rows": len(rows),
                    "formula_count": formula_count,
                    "error_count": len(errors),
                    "training_eligible": False,
                }
            )
            continue
        current_transect: str | None = None
        current_coordinate: dict[str, Any] = {
            "raw": None,
            "latitude": None,
            "longitude": None,
            "status": "missing",
        }
        block_start = 0
        blocks: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        for index, row in enumerate(rows):
            values = [cell.value for cell in row]
            text = " | ".join(_clean_text(value) for value in values if value is not None)
            transect_match = _TRANSECT_RE.search(text)
            if transect_match:
                if current_transect is not None:
                    blocks.append(
                        (
                            block_start,
                            index,
                            {"transect_id": current_transect, "coordinate": current_coordinate},
                            {"rows": rows[block_start:index], "cached": cached_rows[block_start:index]},
                        )
                    )
                current_transect = re.sub(r"\s+", "", transect_match.group("id")).upper()
                block_start = index
                current_coordinate = {"raw": None, "latitude": None, "longitude": None, "status": "missing"}
            coordinate_text = _find_text(values, "koordinat")
            if coordinate_text is not None:
                coordinate_value = (
                    coordinate_text.split(":", 1)[1].strip() if ":" in coordinate_text else coordinate_text
                )
                current_coordinate = parse_dms(coordinate_value)
        if current_transect is not None:
            blocks.append(
                (
                    block_start,
                    len(rows),
                    {"transect_id": current_transect, "coordinate": current_coordinate},
                    {"rows": rows[block_start:], "cached": cached_rows[block_start:]},
                )
            )
        parsed_records = 0
        for start, _end, block_meta, block in blocks:
            block_rows = block["rows"]
            block_cached = block["cached"]
            header_index = next(
                (
                    offset
                    for offset, row in enumerate(block_rows)
                    if "jenis pohon" in " | ".join(_clean_text(cell.value) for cell in row).lower()
                    and (
                        "keliling" in " | ".join(_clean_text(cell.value) for cell in row).lower()
                        or "diameter" in " | ".join(_clean_text(cell.value) for cell in row).lower()
                    )
                ),
                None,
            )
            if header_index is None:
                continue
            headers = _header_columns([cell.value for cell in block_rows[header_index]])
            fraction = _carbon_fraction(
                [
                    (start + offset, [cell.value for cell in row], [cell.value for cell in row])
                    for offset, row in enumerate(block_rows)
                ]
            )
            for offset, row in enumerate(block_rows[header_index + 1 :], header_index + 1):
                values = [cell.value for cell in row]
                cached_values = (
                    [cell.value for cell in block_cached[offset]] if offset < len(block_cached) else []
                )
                species = (
                    values[headers["species"]]
                    if "species" in headers and headers["species"] < len(values)
                    else None
                )
                tree_id = (
                    values[headers["tree_id"]]
                    if "tree_id" in headers and headers["tree_id"] < len(values)
                    else values[0]
                    if values
                    else None
                )
                if (
                    parse_decimal(tree_id) is None
                    or not species
                    or _clean_text(species) in {"-", "Rata-rata", "Total"}
                ):
                    continue
                coordinate = block_meta["coordinate"]
                formula_cells = {
                    cell.coordinate: cell.value
                    for cell in rows[start + offset]
                    if getattr(cell, "coordinate", None)
                }
                cached_cells = (
                    {
                        cell.coordinate: cell.value
                        for cell in cached_rows[start + offset]
                        if getattr(cell, "coordinate", None)
                    }
                    if start + offset < len(cached_rows)
                    else {}
                )
                circumference = (
                    parse_decimal(values[headers["circumference"]])
                    if "circumference" in headers and headers["circumference"] < len(values)
                    else None
                )
                dbh_formula = (
                    values[headers["dbh"]] if "dbh" in headers and headers["dbh"] < len(values) else None
                )
                dbh_cached = (
                    parse_decimal(cached_values[headers["dbh"]])
                    if "dbh" in headers and headers["dbh"] < len(cached_values)
                    else None
                )
                dbh_recalculated = circumference / math.pi if circumference is not None else None
                biomass_formula = (
                    values[headers["biomass"]]
                    if "biomass" in headers and headers["biomass"] < len(values)
                    else None
                )
                biomass_cached = (
                    parse_decimal(cached_values[headers["biomass"]])
                    if "biomass" in headers and headers["biomass"] < len(cached_values)
                    else None
                )
                biomass_recalculated = safe_evaluate_formula(
                    biomass_formula,
                    {
                        **formula_cells,
                        **{key: value for key, value in cached_cells.items() if value is not None},
                    },
                )
                species_meta = normalize_species(species)
                status_value = (
                    values[headers["tree_status"]]
                    if "tree_status" in headers and headers["tree_status"] < len(values)
                    else species
                )
                tree_status_text = _clean_text(status_value).lower()
                tree_status = (
                    "dead"
                    if any(token in tree_status_text for token in ("mati", "dead", "kering"))
                    else "alive"
                )
                flags = []
                if coordinate["status"] != "valid":
                    flags.append("coordinate_" + coordinate["status"])
                if biomass_recalculated is None:
                    flags.append("formula_unresolved")
                if isinstance(biomass_formula, str) and "#REF!" in biomass_formula.upper():
                    flags.append("source_reference_broken")
                if (
                    biomass_cached is not None
                    and biomass_recalculated is not None
                    and not math.isclose(biomass_cached, biomass_recalculated, rel_tol=1e-3, abs_tol=0.01)
                ):
                    flags.append("cached_value_mismatch")
                biomass_difference = (
                    biomass_recalculated - biomass_cached
                    if biomass_recalculated is not None and biomass_cached is not None
                    else None
                )
                biomass_relative_difference = (
                    biomass_difference / abs(biomass_recalculated)
                    if biomass_difference is not None and biomass_recalculated
                    else None
                )
                cached_reconciliation = (
                    "not_computed"
                    if biomass_recalculated is None or biomass_cached is None
                    else "match"
                    if math.isclose(biomass_cached, biomass_recalculated, rel_tol=1e-3, abs_tol=0.01)
                    else "mismatch"
                )
                records.append(
                    {
                        "dataset_id": dataset_id,
                        "campaign_year": campaign_year,
                        "site_id": sheet_name,
                        "plot_id": f"{sheet_name}:{block_meta['transect_id']}",
                        "transect_id": block_meta["transect_id"],
                        "source_workbook": source.name,
                        "source_sheet": sheet_name,
                        "source_row": start + offset + 1,
                        "coordinate_raw": coordinate["raw"],
                        "latitude": coordinate["latitude"],
                        "longitude": coordinate["longitude"],
                        "coordinate_status": coordinate["status"],
                        "species_raw": species_meta["raw"],
                        "scientific_name_normalized": species_meta["scientific_name"],
                        "local_name": species_meta["local_name"],
                        "tree_status": tree_status,
                        "carbon_pool": "agb_carbon",
                        "circumference_cm": circumference,
                        "dbh_cm": dbh_recalculated,
                        "dbh_excel_cached": dbh_cached,
                        "dbh_formula_raw": dbh_formula,
                        "height_cm": parse_decimal(values[headers["height"]])
                        if "height" in headers and headers["height"] < len(values)
                        else None,
                        "agb_biomass_kg": biomass_recalculated,
                        "agb_biomass_excel_cached_kg": biomass_cached,
                        "agb_biomass_difference_kg": biomass_difference,
                        "agb_biomass_relative_difference": biomass_relative_difference,
                        "cached_reconciliation": cached_reconciliation,
                        "formula_mismatch": cached_reconciliation,
                        "cached_value_stale": cached_reconciliation == "mismatch",
                        "unit_mismatch": "not_computed",
                        "area_mismatch": "not_computed",
                        "allometric_equation_raw": biomass_formula,
                        "allometric_equation_id": "whitelisted_excel_algebraic"
                        if biomass_recalculated is not None
                        else None,
                        "allometric_source": _clean_text(values[headers["equation_source"]])
                        if "equation_source" in headers and headers["equation_source"] < len(values)
                        else None,
                        "wood_density": None,
                        "wood_density_status": "embedded_or_not_declared; not inferred",
                        "carbon_fraction": fraction,
                        "agb_carbon_kg": biomass_recalculated * fraction
                        if biomass_recalculated is not None
                        else None,
                        "plot_area_m2": None,
                        "agb_carbon_density_t_ha": None,
                        "vegetated_area_ha": None,
                        "total_agb_carbon_t": None,
                        "unit_reconciliation": {
                            "agb_biomass": "kg/tree",
                            "agb_carbon": "kg/tree",
                            "plot_area": "not_declared",
                            "density": "not_computed",
                        },
                        "validation_flags": flags,
                        "provenance": {
                            "source_workbook": source.name,
                            "sheet": sheet_name,
                            "row": start + offset + 1,
                            "cells": {
                                "circumference": f"{sheet_name}!{getattr(rows[start + offset][headers['circumference']], 'coordinate', None)}"
                                if "circumference" in headers
                                else None,
                                "dbh": f"{sheet_name}!{getattr(rows[start + offset][headers['dbh']], 'coordinate', None)}"
                                if "dbh" in headers
                                else None,
                                "biomass": f"{sheet_name}!{getattr(rows[start + offset][headers['biomass']], 'coordinate', None)}"
                                if "biomass" in headers
                                else None,
                            },
                            "values": {
                                "dbh_formula_raw": dbh_formula,
                                "dbh_cached": dbh_cached,
                                "biomass_formula_raw": biomass_formula,
                                "biomass_cached": biomass_cached,
                            },
                        },
                    }
                )
                parsed_records += 1
        sheet_audits.append(
            {
                "sheet": sheet_name,
                "rows": len(rows),
                "formula_count": formula_count,
                "error_count": len(errors),
                "training_eligible": bool(blocks),
                "transect_count": len(blocks),
                "tree_count": parsed_records,
            }
        )
        global_errors.extend({"sheet": sheet_name, **error} for error in errors[:50])
    formula_wb.close()
    cached_wb.close()
    plot_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    location_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        plot_groups[record["plot_id"]].append(record)
        location_groups[record["site_id"]].append(record)
    plot_reconciliation = []
    for plot_id, plot_records in plot_groups.items():
        valid_records = [record for record in plot_records if not record["validation_flags"]]
        plot_reconciliation.append(
            {
                "plot_id": plot_id,
                "site_id": plot_records[0]["site_id"],
                "campaign_year": plot_records[0]["campaign_year"],
                "tree_count": len(plot_records),
                "valid_tree_count": len(valid_records),
                "agb_biomass_kg_plot": sum(record["agb_biomass_kg"] or 0 for record in valid_records),
                "agb_carbon_kg_plot": sum(record["agb_carbon_kg"] or 0 for record in valid_records),
                "plot_area_m2": None,
                "agb_carbon_density_t_ha": None,
                "status": "valid"
                if valid_records and plot_records[0]["coordinate_status"] == "valid"
                else "review",
            }
        )
    location_reconciliation = [
        {
            "site_id": site_id,
            "campaign_years": sorted({record["campaign_year"] for record in site_records}),
            "plot_count": len({record["plot_id"] for record in site_records}),
            "tree_count": len(site_records),
            "valid_tree_count": sum(1 for record in site_records if not record["validation_flags"]),
            "agb_carbon_kg": sum(
                record["agb_carbon_kg"] or 0 for record in site_records if not record["validation_flags"]
            ),
        }
        for site_id, site_records in sorted(location_groups.items())
    ]
    coordinate_problems = [
        {
            "site_id": record["site_id"],
            "plot_id": record["plot_id"],
            "source_row": record["source_row"],
            "coordinate_raw": record["coordinate_raw"],
            "coordinate_status": record["coordinate_status"],
        }
        for record in records
        if record["coordinate_status"] != "valid"
    ]
    return {
        "dataset_id": dataset_id,
        "source": {
            "filename": source.name,
            "path": str(source),
            "sha256": digest,
            "size_bytes": size,
            "modified_at": dt.datetime.fromtimestamp(source.stat().st_mtime, dt.UTC).isoformat(),
        },
        "sheets": sheet_audits,
        "records": records,
        "audit": {
            "tree_count": len(records),
            "site_count": len({record["site_id"] for record in records}),
            "transect_count": len({(record["site_id"], record["transect_id"]) for record in records}),
            "coordinate_status": dict(Counter(record["coordinate_status"] for record in records)),
            "formula_flags": dict(Counter(flag for record in records for flag in record["validation_flags"])),
            "cached_reconciliation": dict(Counter(record["cached_reconciliation"] for record in records)),
            "tree_valid_count": sum(1 for record in records if not record["validation_flags"]),
            "tree_review_count": sum(1 for record in records if record["validation_flags"]),
            "plot_reconciliation": plot_reconciliation,
            "location_reconciliation": location_reconciliation,
            "coordinate_problems": coordinate_problems,
            "area_audit": area_audit,
            "unit_warnings": [
                "Workbook summary labels include both density (t C/ha) and total-carbon formulas; no summary total was imported as a tree label",
                "Plot area 400 m² appears in formulas but remains unconfirmed",
            ],
            "errors": global_errors,
            "training_excluded_sheets": [
                item["sheet"] for item in sheet_audits if not item["training_eligible"]
            ],
        },
    }


def validate_raster_coverage(
    records: list[dict[str, Any]], bounds: tuple[float, float, float, float], raster_crs: str
) -> dict[str, Any]:
    if raster_crs != "EPSG:4326":
        return {
            "status": "not_computed",
            "reason": "Coverage check requires reprojection to a declared raster CRS",
            "raster_crs": raster_crs,
        }
    west, south, east, north = bounds
    by_plot: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record["plot_id"]
        lat, lon = record["latitude"], record["longitude"]
        coordinate_valid = record["coordinate_status"] == "valid" and lat is not None and lon is not None
        by_plot.setdefault(
            key,
            {
                "plot_id": key,
                "status": "coordinate_invalid" if not coordinate_valid else "outside",
                "latitude": lat,
                "longitude": lon,
            },
        )
        if coordinate_valid and west <= lon <= east and south <= lat <= north:
            by_plot[key]["status"] = (
                "intersecting_boundary" if lon in {west, east} or lat in {south, north} else "inside"
            )
    counts = Counter(item["status"] for item in by_plot.values())
    return {
        "status": "computed",
        "raster_crs": raster_crs,
        "bounds": list(bounds),
        "plot_count": len(by_plot),
        "counts": dict(counts),
        "plots": list(by_plot.values()),
        "blocking": counts.get("outside", 0) > 0 or counts.get("coordinate_invalid", 0) > 0,
    }
