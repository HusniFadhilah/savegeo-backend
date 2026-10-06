"""CAMS product metadata and safe calculations for the wildfire explorer.

The metadata here describes official catalogue products. It does not imply
that this deployment has downloaded CAMS fields. Live retrieval/rendering stays
disabled until ADS access, licence acceptance, and the raster provider are
validated together.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from pyproj import Geod
from shapely.geometry import box, shape

from app.core.config import Settings, get_settings

ADS_HOST = "ads.atmosphere.copernicus.eu"
GFAS_DATASET_ID = "cams-global-fire-emissions-gfas"
IFS_DATASET_ID = "cams-global-atmospheric-composition-forecasts"
GFAS_URL = f"https://{ADS_HOST}/datasets/{GFAS_DATASET_ID}"
IFS_URL = f"https://{ADS_HOST}/datasets/{IFS_DATASET_ID}"
WGS84_GEOD = Geod(ellps="WGS84")
MAX_GRID_CELLS = 100_000

GFAS_VARIABLES = {
    "co2": {
        "label": "Carbon dioxide flux",
        "symbol": "CO₂",
        "source_name": "Wildfire flux of carbon dioxide (CO2)",
        "unit": "kg m-2 s-1",
        "kind": "emission_flux",
    },
    "co": {
        "label": "Carbon monoxide flux",
        "symbol": "CO",
        "source_name": "Wildfire flux of carbon monoxide (CO)",
        "unit": "kg m-2 s-1",
        "kind": "emission_flux",
    },
    "pm25": {
        "label": "Particulate matter flux (diameter < 2.5 µm)",
        "symbol": "PM2.5",
        "source_name": "Wildfire flux of particulate matter d < 2.5 µm (PM2.5)",
        "unit": "kg m-2 s-1",
        "kind": "emission_flux",
    },
    "burnt_carbon": {
        "label": "Overall flux of burnt carbon",
        "symbol": "C",
        "source_name": "Wildfire overall flux of burnt carbon",
        "unit": "kg m-2 s-1",
        "kind": "emission_flux",
    },
    "frp": {
        "label": "Fire radiative power density",
        "symbol": "FRP",
        "source_name": "Wildfire radiative power",
        "unit": "W m-2",
        "kind": "power_density",
    },
}

PRODUCT_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "gfas",
        "label": "GFAS fire emissions",
        "dataset_id": GFAS_DATASET_ID,
        "source_url": GFAS_URL,
        "provider": "CAMS / ECMWF",
        "license": "CC-BY-4.0",
        "doi": "10.24381/a05253c7",
        "resolution_degrees": 0.1,
        "spatial_coverage": "global regular latitude-longitude grid",
        "time_basis": "UTC",
        "products": [
            {
                "version": "1.2",
                "coverage_start": "2003-01-01",
                "coverage_end": "2025-12-31",
                "temporal_steps": ["daily"],
                "notes": "Daily average; archived version.",
            },
            {
                "version": "1.4.2",
                "coverage_start": "2026-01-01",
                "coverage_end": None,
                "temporal_steps": ["hourly", "rolling_24h"],
                "notes": "Hourly estimates and 24-hour rolling averages; availability may lag valid time.",
            },
        ],
        "variables": GFAS_VARIABLES,
        "method": "FRP observations are converted to dry matter consumed, then emission factors estimate species fluxes.",
        "data_kind": "fire-source emission estimates; not surface concentration, burned area, or hotspot counts",
    },
    {
        "id": "ifs_cams",
        "label": "IFS-CAMS atmospheric composition forecast",
        "dataset_id": IFS_DATASET_ID,
        "source_url": IFS_URL,
        "provider": "CAMS / ECMWF",
        "license": "CC-BY-4.0",
        "doi": "10.24381/04a0b097",
        "resolution_degrees": 0.4,
        "spatial_coverage": "global gridded forecast",
        "time_basis": "UTC",
        "initialization_hours_utc": [0, 12],
        "forecast_horizon_hours": 120,
        "single_level_step_hours": 1,
        "variables": {
            "pm25": {
                "label": "Particulate matter concentration (diameter < 2.5 µm)",
                "symbol": "PM2.5",
                "source_name": "Particulate matter d < 2.5 µm (PM2.5)",
                "unit": "kg m-3",
                "kind": "atmospheric_concentration",
                "temporal_resolution_hours": 1,
                "vertical_product": "single_level",
            },
            "co": {
                "label": "Carbon monoxide mass mixing ratio",
                "symbol": "CO",
                "source_name": "Carbon monoxide",
                "unit": "kg kg-1",
                "kind": "mass_mixing_ratio",
                "temporal_resolution_hours": 3,
                "vertical_product": "multi_level; select lowest model level (137 since 2019-07-07, otherwise 60)",
            },
        },
        "method": "Atmospheric analysis and model forecast using multiple emissions and assimilation inputs.",
        "data_kind": "atmospheric forecast; not fire-only attribution, a satellite image, or a ground measurement",
    },
)


class CamsRequestError(ValueError):
    """A safe, client-displayable CAMS request error."""

    def __init__(self, message: str, status_code: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class CamsFieldProvider(Protocol):
    """Boundary for a future ADS-backed reader or a deterministic test fixture."""

    def read_field(self, request: dict[str, str]) -> "GridField | None": ...


class CdsApiArchiveAdapter:
    """Thin, allowlisted transport wrapper around the official CDS API client."""

    def __init__(self, client: Any):
        self._client = client

    def retrieve(self, dataset_id: str, request: dict[str, Any], target: str | Path) -> Path:
        if dataset_id not in {GFAS_DATASET_ID, IFS_DATASET_ID}:
            raise CamsRequestError("Dataset ADS tidak diizinkan", code="unsupported_dataset")
        if not isinstance(request, dict) or not request:
            raise CamsRequestError("Parameter permintaan ADS tidak valid")
        target_path = Path(target)
        try:
            self._client.retrieve(dataset_id, request, str(target_path))
        except Exception:
            # ADS client errors may include request URLs or authentication
            # details. Keep only a stable error code/message for API and logs.
            raise CamsRequestError(
                "Permintaan ke CAMS ADS gagal; periksa akses akun, lisensi, dan parameter produk",
                502,
                "ads_request_failed",
            ) from None
        return target_path


def create_ads_archive_adapter(
    settings: Settings | None = None,
    client_factory: Any | None = None,
) -> CdsApiArchiveAdapter:
    """Build the official CDS API transport only after server checks pass.

    Importing this function does not import the optional CDS API package or
    perform a network request. It is not wired to public routes until a
    downloaded-file parser and live data validation are complete.
    """
    settings = settings or get_settings()
    capability = get_capabilities(settings)
    if not settings.cams_ads_api_key:
        raise CamsRequestError("CAMS ADS belum memiliki kredensial server", 503, "ads_credentials_missing")
    if capability["status"] == "invalid_configuration":
        raise CamsRequestError(
            "CAMS ADS harus menggunakan host HTTPS resmi", 503, "invalid_ads_configuration"
        )
    if not settings.cams_ads_terms_accepted:
        raise CamsRequestError(
            "Persetujuan lisensi dataset CAMS ADS belum dikonfirmasi", 503, "terms_not_accepted"
        )
    if client_factory is None:
        try:
            import cdsapi
        except ImportError:
            raise CamsRequestError(
                "Pasang extra backend [cams] untuk mengaktifkan client ADS", 503, "cdsapi_missing"
            ) from None
        client_factory = cdsapi.Client
    try:
        client = client_factory(url=settings.cams_ads_api_url, key=settings.cams_ads_api_key, quiet=True)
    except Exception:
        raise CamsRequestError(
            "Client CAMS ADS tidak dapat dikonfigurasi", 503, "ads_client_configuration_failed"
        ) from None
    return CdsApiArchiveAdapter(client)


@dataclass(frozen=True)
class FluxCell:
    west: float
    south: float
    east: float
    north: float
    value: float | None


@dataclass(frozen=True)
class GridField:
    product: str
    version: str
    variable: str
    valid_at: datetime
    unit: str
    cells: tuple[FluxCell, ...]
    nodata: float | None = None
    temporal_step: str = "hourly"


def get_capabilities(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    parsed_api_url = urlparse(settings.cams_ads_api_url)
    api_url_valid = (
        parsed_api_url.scheme == "https"
        and parsed_api_url.hostname == ADS_HOST
        and parsed_api_url.username is None
        and parsed_api_url.password is None
    )
    credentials_configured = bool(settings.cams_ads_api_key) and api_url_valid
    if not api_url_valid:
        status = "invalid_configuration"
        blocked_reason = "ads_url_must_use_official_host"
    elif not settings.cams_ads_api_key:
        status = "needs_credentials"
        blocked_reason = "ads_credentials_missing"
    elif not settings.cams_ads_terms_accepted:
        status = "needs_terms_acceptance"
        blocked_reason = "dataset_terms_not_confirmed"
    else:
        status = "upstream_validation_blocked"
        blocked_reason = "provider_and_live_data_validation_pending"
    return {
        "status": status,
        "credentials_configured": credentials_configured,
        "terms_accepted_by_operator": bool(settings.cams_ads_terms_accepted),
        "provider_ready": False,
        "data_available": False,
        "blocked_reason": blocked_reason,
        "integration_stage": "ads_transport_and_fixture_math; raster_provider_unverified",
        "products": [dict(product) for product in PRODUCT_CATALOG],
        "attribution": "CAMS / ECMWF Atmosphere Data Store (ADS), CC-BY 4.0",
        "disclaimer": "Metadata describes catalogue products. No CAMS field is currently loaded for display.",
    }


def validate_request(product: str, version: str, variable: str, valid_at: str) -> dict[str, str]:
    product = product.strip().lower()
    version = version.strip()
    variable = variable.strip().lower()
    try:
        parsed = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CamsRequestError("valid_at harus berupa tanggal/waktu ISO-8601 UTC") from exc
    if parsed.tzinfo is None:
        raise CamsRequestError("valid_at harus menyertakan zona waktu UTC")
    parsed_utc = parsed.astimezone(UTC)
    day = parsed_utc.date()
    now = datetime.now(UTC)
    if parsed_utc > now:
        raise CamsRequestError("Waktu valid tidak boleh berada di masa depan", code="future_valid_time")

    if product == "gfas":
        product_version = next(
            (entry for entry in PRODUCT_CATALOG[0]["products"] if entry["version"] == version), None
        )
        if product_version is None:
            raise CamsRequestError("Versi GFAS tidak tersedia dalam katalog", code="unsupported_version")
        if variable not in GFAS_VARIABLES:
            raise CamsRequestError("Variabel GFAS tidak tersedia dalam katalog", code="unsupported_variable")
        start = date.fromisoformat(product_version["coverage_start"])
        end_value = product_version["coverage_end"]
        end = date.fromisoformat(end_value) if end_value else None
        if day < start or (end and day > end):
            raise CamsRequestError(
                "Tanggal berada di luar rentang versi GFAS yang dipilih", code="date_outside_coverage"
            )
        if version == "1.2" and (
            parsed_utc.hour or parsed_utc.minute or parsed_utc.second or parsed_utc.microsecond
        ):
            raise CamsRequestError("GFAS 1.2 hanya memiliki langkah harian", code="invalid_valid_time")
        if version == "1.4.2" and (parsed_utc.minute or parsed_utc.second or parsed_utc.microsecond):
            raise CamsRequestError("GFAS 1.4.2 hanya memiliki waktu valid per jam", code="invalid_valid_time")
    elif product == "ifs_cams":
        if version != "current":
            raise CamsRequestError("Versi IFS-CAMS mengikuti versi model aktif", code="unsupported_version")
        if variable not in PRODUCT_CATALOG[1]["variables"]:
            raise CamsRequestError(
                "Variabel IFS-CAMS tidak tersedia dalam katalog", code="unsupported_variable"
            )
        if day < date(2015, 1, 1):
            raise CamsRequestError("Tanggal berada di luar cakupan IFS-CAMS", code="date_outside_coverage")
        if parsed_utc > now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=120):
            raise CamsRequestError(
                "Waktu valid melebihi horizon prakiraan IFS-CAMS", code="forecast_horizon_exceeded"
            )
        step_hours = PRODUCT_CATALOG[1]["variables"][variable]["temporal_resolution_hours"]
        if parsed_utc.minute or parsed_utc.second or parsed_utc.microsecond or parsed_utc.hour % step_hours:
            raise CamsRequestError(
                f"Waktu IFS-CAMS harus mengikuti langkah {step_hours} jam UTC untuk variabel ini",
                code="invalid_valid_time",
            )
    else:
        raise CamsRequestError("Produk CAMS tidak valid", code="unsupported_product")
    return {
        "product": product,
        "version": version,
        "variable": variable,
        "valid_at": parsed_utc.isoformat().replace("+00:00", "Z"),
    }


def normalize_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CamsRequestError("Waktu harus berupa ISO-8601") from exc
    if value.tzinfo is None:
        raise CamsRequestError("Waktu harus menyertakan zona waktu UTC")
    return value.astimezone(UTC)


def geodesic_cell_area_m2(west: float, south: float, east: float, north: float) -> float:
    """Ellipsoidal area of a latitude/longitude cell in square metres."""
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise CamsRequestError("Koordinat sel tidak valid")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise CamsRequestError("Batas sel tidak valid")
    area, _ = WGS84_GEOD.polygon_area_perimeter([west, east, east, west], [south, south, north, north])
    return abs(area)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    # Divide before summing so a representable mean does not overflow merely
    # because a naive intermediate sum does.
    result = math.fsum(value / len(values) for value in values)
    return result if math.isfinite(result) else None


def clipped_cell_area_m2(cell: FluxCell, aoi_geojson: dict[str, Any] | None = None) -> float:
    geodesic_cell_area_m2(cell.west, cell.south, cell.east, cell.north)
    cell_polygon = box(cell.west, cell.south, cell.east, cell.north)
    if aoi_geojson is None:
        clipped = cell_polygon
    else:
        geometry = aoi_geojson.get("geometry") if aoi_geojson.get("type") == "Feature" else aoi_geojson
        try:
            clipped = cell_polygon.intersection(shape(geometry))
        except Exception as exc:
            raise CamsRequestError("Geometri AOI tidak valid") from exc
    if clipped.is_empty:
        return 0.0
    area, _ = WGS84_GEOD.geometry_area_perimeter(clipped)
    return abs(area)


def integrate_flux_cells(
    cells: tuple[FluxCell, ...] | list[FluxCell],
    duration_seconds: float,
    aoi_geojson: dict[str, Any] | None = None,
    nodata: float | None = None,
) -> dict[str, float | int | None]:
    """Integrate a surface flux to mass using clipped cell area and real duration.

    ``kg m-2 s-1 * m2 * s = kg``. Missing/nodata cells are excluded from
    max/mean and contribute no assumed zero mass.
    """
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise CamsRequestError("Durasi integrasi harus lebih besar dari nol")
    if len(cells) > MAX_GRID_CELLS:
        raise CamsRequestError("Grid melampaui batas jumlah sel untuk satu permintaan", 413, "grid_too_large")
    values: list[float] = []
    total_kg = 0.0
    missing_cells = 0
    for cell in cells:
        area = clipped_cell_area_m2(cell, aoi_geojson)
        if area <= 0:
            continue
        value = cell.value
        if value is None or not math.isfinite(value) or (nodata is not None and value == nodata):
            missing_cells += 1
            continue
        if value < 0:
            raise CamsRequestError("Flux emisi GFAS tidak boleh negatif")
        mass = value * area * duration_seconds
        if not math.isfinite(mass):
            raise CamsRequestError("Nilai flux menghasilkan total di luar rentang numerik")
        values.append(value)
        total_kg += mass
        if not math.isfinite(total_kg):
            raise CamsRequestError("Total emisi di luar rentang numerik")
    return {
        "valid_cell_count": len(values),
        "missing_cell_count": missing_cells,
        "max_flux_kg_m2_s": max(values) if values else None,
        "mean_flux_kg_m2_s": _mean(values),
        "integrated_mass_kg": total_kg if values else None,
        "duration_seconds": duration_seconds,
    }


def aggregate_frp_density(
    cells: tuple[FluxCell, ...] | list[FluxCell], aoi_geojson: dict[str, Any] | None = None
) -> dict[str, float | int | None]:
    """Convert GFAS FRP density (W m-2) to total power (W), without time integration."""
    if len(cells) > MAX_GRID_CELLS:
        raise CamsRequestError("Grid melampaui batas jumlah sel untuk satu permintaan", 413, "grid_too_large")
    values: list[float] = []
    total_w = 0.0
    for cell in cells:
        area = clipped_cell_area_m2(cell, aoi_geojson)
        if area <= 0 or cell.value is None or not math.isfinite(cell.value):
            continue
        if cell.value < 0:
            raise CamsRequestError("FRP density tidak boleh negatif")
        values.append(cell.value)
        total_w += cell.value * area
        if not math.isfinite(total_w):
            raise CamsRequestError("Total FRP di luar rentang numerik")
    return {
        "valid_cell_count": len(values),
        "max_frp_density_w_m2": max(values) if values else None,
        "mean_frp_density_w_m2": _mean(values),
        "total_frp_w": total_w if values else None,
    }


def build_cache_key(request: dict[str, str], region_id: str, style: dict[str, Any] | None = None) -> str:
    """Stable digest for product/version/variable/time/region/style cache scopes."""
    canonical = json.dumps(
        {"request": request, "region_id": region_id, "style": style or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize_field(
    field: GridField,
    duration_seconds: float | None,
    aoi_geojson: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = validate_request(
        field.product, field.version, field.variable, normalize_utc(field.valid_at).isoformat()
    )
    variable = GFAS_VARIABLES.get(field.variable) if field.product == "gfas" else None
    if variable and variable["kind"] == "emission_flux":
        if field.unit != variable["unit"]:
            raise CamsRequestError("Satuan field tidak cocok dengan metadata variabel")
        if duration_seconds is None:
            raise CamsRequestError("Durasi harus ditentukan untuk integrasi flux")
        expected_durations = {
            "1.2": {"daily": 86_400},
            "1.4.2": {"hourly": 3_600, "rolling_24h": 86_400},
        }
        expected_duration = expected_durations.get(field.version, {}).get(field.temporal_step)
        if expected_duration is None or duration_seconds != expected_duration:
            raise CamsRequestError("Durasi integrasi tidak cocok dengan langkah waktu GFAS")
        values = integrate_flux_cells(field.cells, duration_seconds, aoi_geojson, field.nodata)
        return {
            "request": request,
            "unit": "kg",
            "method": "flux × clipped cell area × interval seconds",
            **values,
        }
    if variable and variable["kind"] == "power_density":
        if field.unit != variable["unit"]:
            raise CamsRequestError("Satuan field tidak cocok dengan metadata variabel")
        values = aggregate_frp_density(field.cells, aoi_geojson)
        return {
            "request": request,
            "unit": "W",
            "method": "FRP density × clipped cell area; no time integration",
            **values,
        }
    raise CamsRequestError(
        "Ringkasan ini hanya mendukung field emisi GFAS yang terverifikasi", 503, "provider_not_ready"
    )


def summarize_request(
    provider: CamsFieldProvider,
    request: dict[str, str],
    duration_seconds: float | None,
    aoi_geojson: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one normalized field through the provider boundary and summarize it.

    Production callers must supply a provider that only returns ADS files whose
    request, version, valid time, and units have already been checked. Tests use
    a tiny in-memory implementation of the same interface.
    """
    normalized = validate_request(
        request.get("product", ""),
        request.get("version", ""),
        request.get("variable", ""),
        request.get("valid_at", ""),
    )
    field = provider.read_field(normalized)
    if field is None:
        raise CamsRequestError("Data CAMS tidak tersedia untuk waktu yang diminta", 503, "data_unavailable")
    actual = {
        "product": field.product,
        "version": field.version,
        "variable": field.variable,
        "valid_at": normalize_utc(field.valid_at).isoformat().replace("+00:00", "Z"),
    }
    if actual != normalized:
        raise CamsRequestError(
            "Provenance field tidak cocok dengan permintaan", 502, "provider_provenance_mismatch"
        )
    return summarize_field(field, duration_seconds, aoi_geojson)
