"""Parsing and row validation for dMRV field-data imports (pure; no database).

Nothing is guessed silently: unit conversion of height is an explicit option,
placeholders such as ``-`` become missing (never 0), and every suspicious value
is reported with the row number so the field team can correct the source.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_ROWS = 50_000
MAX_COLUMNS = 200
# Column widths of the target tables (dmrv_survey_plots / dmrv_tree_measurements / dmrv_survey_visits).
MAX_LENGTHS = {"plot_code": 64, "tree_code": 64, "species": 255, "operator": 128}
_AMBIGUOUS_NUMBER = re.compile(r"^[1-9]\d{0,2}[.,]\d{3}$")
_AMBIGUITY_CHECKED = frozenset({"circumference_cm", "dbh_cm", "height", "biomass_kg", "plot_area_m2"})
ALLOWED_STATUS = frozenset({"live", "dead", "recruit", "missing"})
_MISSING_TOKENS = frozenset({"", "-", "na", "n/a", "nan", "null"})
_INTEGRAL_FLOAT = re.compile(r"^(\d+)\.0+$")

CANONICAL_FIELDS: tuple[str, ...] = (
    "plot_code", "visit_date", "survey_year", "tree_code", "species", "circumference_cm", "dbh_cm",
    "height", "biomass_kg", "status", "latitude", "longitude", "plot_area_m2", "operator",
)
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "plot_code": ("plot_code", "plot", "point id", "titik", "id plot"),
    "visit_date": ("visit_date", "date", "tanggal"),
    "survey_year": ("survey_year", "year", "tahun"),
    "tree_code": ("tree_code", "tree id", "no.", "no"),
    "species": ("species", "jenis pohon", "jenis", "spesies"),
    "circumference_cm": ("circumference_cm", "keliling (cm)", "keliling"),
    "dbh_cm": ("dbh_cm", "dbh", "diameter (cm)", "diameter"),
    "height": ("height", "tinggi", "tinggi(cm)", "tinggi (cm)", "tinggi (m)"),
    "biomass_kg": ("biomass_kg", "biomassa (kg)", "biomassa"),
    "status": ("status",),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng", "long"),
    "plot_area_m2": ("plot_area_m2", "luas plot (m2)", "plot area"),
    "operator": ("operator", "surveyor"),
}


class FieldImportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ImportOptions:
    height_unit: Literal["m", "cm"]
    plot_area_m2: float | None = None
    area_basis: Literal["measured", "assumed"] | None = None
    default_visit_year: int | None = None


@dataclass
class RowIssue:
    row: int
    level: Literal["error", "warning"]
    code: str
    message: str


@dataclass
class ParsedTree:
    row: int
    plot_code: str
    visit_date: dt.date
    date_precision: str
    tree_code: str
    species: str | None
    circumference_cm: float | None
    dbh_cm: float | None
    height_m: float | None
    biomass_kg: float | None
    status: str
    latitude: float | None
    longitude: float | None
    area_m2: float | None
    area_basis: str | None
    operator: str | None
    flags: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    trees: list[ParsedTree]
    issues: list[RowIssue]
    rows_total: int

    @property
    def error_rows(self) -> set[int]:
        return {issue.row for issue in self.issues if issue.level == "error"}


def _normalise_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    by_normal: dict[str, str] = {}
    for header in headers:
        by_normal.setdefault(_normalise_header(header), header)
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for canonical in CANONICAL_FIELDS:
        for alias in COLUMN_ALIASES[canonical]:
            candidate_header = by_normal.get(alias)
            if candidate_header is not None and candidate_header not in used:
                mapping[canonical] = candidate_header
                used.add(candidate_header)
                break
    return mapping


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_csv(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FieldImportError("bad_encoding", "Berkas CSV harus berenkode UTF-8.") from exc
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max((",", ";", "\t"), key=lambda candidate: first_line.count(candidate))
    if first_line.count(delimiter) == 0:
        delimiter = ","
    table: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text), delimiter=delimiter):
            table.append([cell.strip() for cell in row[: MAX_COLUMNS + 1]])
            if len(table) > MAX_ROWS + 2:  # stop early; read_table reports too_many_rows
                break
    except csv.Error as exc:
        raise FieldImportError("bad_csv", f"Berkas CSV tidak valid: {exc}") from exc
    return table


def _read_xlsx(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    table: list[list[str]] = []
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheet = workbook.worksheets[0]
        for row in sheet.iter_rows(values_only=True, max_col=MAX_COLUMNS + 1):
            cells = [_cell_text(cell) for cell in row]
            while cells and not cells[-1]:  # max_col pads every row with empty cells
                cells.pop()
            table.append(cells)
            if len(table) > MAX_ROWS + 2:  # a decompression bomb never gets fully materialised
                break
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for corrupt files
        raise FieldImportError("unreadable_file", "Berkas XLSX tidak dapat dibaca.") from exc
    return table


def read_table(filename: str, data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    lowered = filename.lower()
    if lowered.endswith(".csv"):
        table = _read_csv(data)
    elif lowered.endswith(".xlsx"):
        table = _read_xlsx(data)
    else:
        raise FieldImportError("unsupported_file", "Hanya berkas .csv dan .xlsx yang didukung.")
    table = [row for row in table if any(cell for cell in row)]
    if len(table) < 2:
        raise FieldImportError("empty_file", "Berkas tidak berisi baris data.")
    if len(table) - 1 > MAX_ROWS:
        raise FieldImportError("too_many_rows", f"Maksimal {MAX_ROWS:,} baris data per berkas.")
    header_row = table[0]
    if len(header_row) > MAX_COLUMNS:
        raise FieldImportError("too_many_columns", f"Maksimal {MAX_COLUMNS} kolom per berkas.")
    columns = [(index, name) for index, name in enumerate(header_row) if name]
    headers = [name for _, name in columns]
    rows = [{name: (row[index] if index < len(row) else "") for index, name in columns} for row in table[1:]]
    return headers, rows


def _parse_number(text: str) -> float | None:
    text = text.strip().replace(" ", "")
    if text.lower() in _MISSING_TOKENS:
        return None
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    elif "," in text:
        text = text.replace(",", ".")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def _clean_code(text: str) -> str:
    text = text.strip()
    match = _INTEGRAL_FLOAT.match(text)
    return match.group(1) if match else text


def _parse_date(text: str) -> dt.date:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(text)


def validate_rows(rows: list[dict[str, str]], mapping: dict[str, str], options: ImportOptions) -> ImportReport:
    trees: list[ParsedTree] = []
    issues: list[RowIssue] = []
    seen: set[tuple[str, dt.date, str]] = set()

    for index, row in enumerate(rows, start=2):
        row_issues: list[RowIssue] = []
        flags: list[str] = []

        def error(code: str, message: str, row_number: int = index, bucket: list[RowIssue] = row_issues) -> None:
            bucket.append(RowIssue(row_number, "error", code, message))

        def warning(code: str, message: str, row_number: int = index, bucket: list[RowIssue] = row_issues) -> None:
            bucket.append(RowIssue(row_number, "warning", code, message))

        def raw(name: str, source: dict[str, str] = row) -> str:
            return source.get(mapping[name], "").strip() if name in mapping else ""

        def number(name: str, *, allow_negative: bool = False, flag_bucket: list[str] = flags) -> float | None:
            try:
                value = _parse_number(raw(name))
            except ValueError:
                error("bad_number", f"Kolom {name} bukan angka: {raw(name)!r}.")
                return None
            if name in _AMBIGUITY_CHECKED and _AMBIGUOUS_NUMBER.match(raw(name).replace(" ", "")):
                warning(
                    "ambiguous_number_format",
                    f"Kolom {name}: {raw(name)!r} bisa berarti ribuan (Indonesia) atau desimal; dibaca sebagai desimal ({value:g}). "
                    "Tulis ulang tanpa pemisah jika maksudnya ribuan.",
                )
                flag_bucket.append("ambiguous_number_format")
            if value is not None and value < 0 and not allow_negative:
                error("negative_value", f"Kolom {name} tidak boleh negatif.")
                return None
            return value

        plot_code = _clean_code(raw("plot_code"))
        if not plot_code:
            error("missing_plot_code", "Kode plot kosong.")

        visit_date: dt.date | None = None
        precision = "day"
        if raw("visit_date"):
            try:
                visit_date = _parse_date(raw("visit_date"))
            except ValueError:
                error("bad_date", f"Tanggal tidak dikenali: {raw('visit_date')!r} (gunakan YYYY-MM-DD atau DD/MM/YYYY).")
        else:
            year_text = raw("survey_year")
            year: int | None = None
            if year_text:
                try:
                    parsed_year = _parse_number(year_text)
                    if parsed_year is None or not parsed_year.is_integer():
                        raise ValueError(year_text)
                    year = int(parsed_year)
                except (ValueError, OverflowError):
                    error("bad_date", f"Tahun survei tidak valid: {year_text!r}.")
            elif options.default_visit_year is not None:
                year = options.default_visit_year
            else:
                error("missing_date", "Tanggal atau tahun survei tidak ada.")
            if year is not None:
                if 1900 <= year <= 2100:
                    visit_date, precision = dt.date(year, 1, 1), "year"
                else:
                    error("bad_date", f"Tahun survei di luar rentang: {year}.")

        tree_code = _clean_code(raw("tree_code"))
        if not tree_code:
            tree_code = f"row-{index}"
            warning("tree_code_generated", f"Kode pohon dibuat otomatis: {tree_code}.")
            flags.append("tree_code_generated")

        if any("\x00" in raw(name) for name in CANONICAL_FIELDS):
            error("bad_text", "Berkas mengandung karakter NUL (\x00) yang tidak dapat disimpan.")
        for name, value in (("plot_code", plot_code), ("tree_code", tree_code), ("species", raw("species")), ("operator", raw("operator"))):
            if len(value) > MAX_LENGTHS[name]:
                error("value_too_long", f"Kolom {name} melebihi {MAX_LENGTHS[name]} karakter.")

        circumference = number("circumference_cm")
        dbh = number("dbh_cm")
        biomass = number("biomass_kg")
        height = number("height")
        if circumference is None and dbh is None and not any(i.code in {"bad_number", "negative_value"} for i in row_issues):
            error("missing_size", "Keliling dan diameter sama-sama kosong.")
        if circumference and dbh and abs(dbh - circumference / math.pi) > 0.05 * dbh:
            warning("dbh_circumference_mismatch", f"DBH {dbh} cm tidak sesuai keliling {circumference} cm (≈{circumference / math.pi:.2f} cm).")
            flags.append("dbh_circumference_mismatch")
        if height is not None:
            if options.height_unit == "cm":
                height = height / 100
            if height > 120:
                warning("height_suspect_unit", f"Tinggi {height:g} m tidak wajar; periksa satuan kolom tinggi.")
                flags.append("height_suspect_unit")

        latitude = number("latitude", allow_negative=True)
        longitude = number("longitude", allow_negative=True)
        if (latitude is None) != (longitude is None) and not any(i.code == "bad_number" for i in row_issues):
            error("incomplete_coordinates", "Latitude dan longitude harus diisi bersamaan.")
        elif latitude is not None and longitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            error("bad_coordinates", f"Koordinat di luar rentang (lat {latitude}, lon {longitude}); cek apakah lat/lon tertukar.")

        status = raw("status").lower() or "live"
        if status not in ALLOWED_STATUS:
            error("bad_status", f"Status tidak dikenal: {status!r}.")

        area: float | None = number("plot_area_m2")
        basis: str | None = "measured"
        if area is not None and area <= 0:
            error("bad_number", "Luas plot harus lebih dari nol.")
        if area is None:
            area, basis = (options.plot_area_m2, options.area_basis or "assumed") if options.plot_area_m2 else (None, None)

        if visit_date is not None and plot_code:
            key = (plot_code, visit_date, tree_code)
            if key in seen:
                error("duplicate_row", f"Pohon {tree_code} pada plot {plot_code} tanggal {visit_date} muncul lebih dari sekali.")

        issues.extend(row_issues)
        if any(issue.level == "error" for issue in row_issues):
            continue
        if visit_date is None:
            # Invalid/missing dates are reported above; never construct a tree
            # record that would violate the persisted visit-date contract.
            continue
        key = (plot_code, visit_date, tree_code)
        seen.add(key)
        trees.append(ParsedTree(
            row=index, plot_code=plot_code, visit_date=visit_date, date_precision=precision, tree_code=tree_code,
            species=raw("species") or None, circumference_cm=circumference, dbh_cm=dbh, height_m=height,
            biomass_kg=biomass, status=status, latitude=latitude, longitude=longitude, area_m2=area,
            area_basis=basis, operator=raw("operator") or None, flags=flags,
        ))
    return ImportReport(trees=trees, issues=issues, rows_total=len(rows))
