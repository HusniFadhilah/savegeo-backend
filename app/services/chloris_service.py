"""Chloris API/download helpers for carbon stock reference rasters."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CHLORIS_S3_PREFIX = "s3://chloris-app-data/"
DEFAULT_PRODUCT = "stock"
PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_CHLORIS_COLLECTION = "chloris-biomass"
PC_CHLORIS_YEAR_RANGE = (2003, 2019)
CHLORIS_LICENSED_YEAR_RANGE = (2000, 2025)
PC_DATA_API_URL = "https://planetarycomputer.microsoft.com/api/data/v1"
CHLORIS_BIOMASS_TO_CARBON_FRACTION = 0.47
_DOWNLOADS_CACHE: dict[tuple, tuple[float, Any]] = {}
_DOWNLOADS_CACHE_LOCK = threading.Lock()


class ChlorisUnavailableError(ValueError):
    """A requested Chloris vintage cannot be accessed with this configuration."""


@dataclass(frozen=True)
class ChlorisDownload:
    """Resolved Chloris downloadable raster."""

    url: str
    product: str
    date: str | None
    format: str | None
    metadata: dict[str, Any]


def is_chloris_configured() -> bool:
    """Return True if enough non-password config exists to try a Chloris raster load."""
    status = chloris_config_status()
    has_direct_path = status["has_data_path"] or _has_direct_download_url()
    has_api_path = status["has_organization_id"] and (status["has_id_token"] or status["has_refresh_token"])
    # The public Planetary Computer collection is a usable fallback when a
    # licensed Chloris reporting unit has not been configured.
    return bool(has_direct_path or has_api_path or status["has_planetary_computer_fallback"])


def chloris_config_status() -> dict[str, Any]:
    """Return non-secret Chloris configuration status for diagnostics."""
    settings = get_settings()
    return {
        "base_url": settings.chloris_base_url.rstrip("/"),
        "has_organization_id": bool(settings.chloris_organization_id),
        "has_reporting_unit_id": bool(settings.chloris_reporting_unit_id),
        "has_id_token": bool(settings.chloris_id_token),
        "has_refresh_token": bool(settings.chloris_refresh_token),
        "has_data_path": bool(settings.chloris_data_path),
        "has_direct_download_url": _has_direct_download_url(),
        "has_planetary_computer_fallback": is_planetary_computer_available(),
        "public_year_range": list(PC_CHLORIS_YEAR_RANGE),
        "licensed_product_year_range": list(CHLORIS_LICENSED_YEAR_RANGE),
    }


def _direct_downloads(product: str) -> dict[int, str]:
    """Read explicitly dated rasters; a fixed URL never inherits the requested year."""
    settings = get_settings()
    prefixes = [f"CHLORIS_{product.upper()}"]
    if product == DEFAULT_PRODUCT:
        prefixes.append("CHLORIS_AGB_STOCK")
    dated: dict[int, str] = {}
    for prefix in reversed(prefixes):
        for name, value in os.environ.items():
            match = re.fullmatch(rf"{prefix}_(\d{{4}})_URL", name)
            if match and value:
                dated[int(match.group(1))] = value
    if product == DEFAULT_PRODUCT:
        for year in (2024, 2025):
            value = getattr(settings, f"chloris_stock_{year}_url")
            if value:
                dated[year] = value
        template = settings.chloris_stock_url_template
        if template and "{year}" in template:
            for value in settings.chloris_stock_years.split(","):
                if value.strip():
                    dated[int(value.strip())] = template.format(year=int(value.strip()))
        fixed_url = settings.chloris_stock_url or settings.chloris_agb_stock_url
        if fixed_url and settings.chloris_stock_year is not None:
            dated[settings.chloris_stock_year] = fixed_url
    return dated


def _has_licensed_index_config() -> bool:
    settings = get_settings()
    return bool(settings.chloris_data_path or (
        settings.chloris_organization_id
        and (settings.chloris_id_token or settings.chloris_refresh_token)
    ))


def _licensed_downloads_index() -> Any:
    """Cache the licensed index for five minutes, including short failure caching."""
    settings = get_settings()
    cache_key = (settings.chloris_base_url, settings.chloris_data_path,
                 settings.chloris_organization_id, settings.chloris_reporting_unit_id,
                 settings.chloris_id_token, settings.chloris_refresh_token)
    with _DOWNLOADS_CACHE_LOCK:
        cached = _DOWNLOADS_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            if isinstance(cached[1], Exception):
                raise cached[1]
            return cached[1]
        try:
            downloads = _request_json(_join_url(_get_data_path(), "downloads.json"))
        except Exception as exc:
            _DOWNLOADS_CACHE[cache_key] = (time.monotonic() + 30, exc)
            raise
        _DOWNLOADS_CACHE.clear()
        _DOWNLOADS_CACHE[cache_key] = (time.monotonic() + 300, downloads)
        return downloads


def chloris_availability(product: str = DEFAULT_PRODUCT) -> dict[str, Any]:
    """Report actual configured/index years without exposing URLs or credentials."""
    product = (product or DEFAULT_PRODUCT).strip().lower()
    licensed_years = set(_direct_downloads(product))
    configuration_note = None
    if _has_licensed_index_config():
        try:
            for entry in _extract_download_entries(_licensed_downloads_index()):
                if (_entry_product(entry) == product and entry.get("url")
                        and _entry_format(entry) in {"tif", "tiff", "geotiff", "cog", ""}):
                    year = _entry_year(entry)
                    if year is not None:
                        licensed_years.add(year)
        except Exception as exc:
            logger.warning("Chloris index unavailable (%s)", type(exc).__name__)
            configuration_note = "Indeks Chloris berlisensi belum dapat diakses; periksa token dan hak unduh raster."
    elif _has_direct_download_url() and not licensed_years:
        configuration_note = "Isi CHLORIS_STOCK_YEAR untuk URL tunggal, atau CHLORIS_STOCK_YEARS untuk template URL."
    public_years = list(range(PC_CHLORIS_YEAR_RANGE[0], PC_CHLORIS_YEAR_RANGE[1] + 1)) if (
        product == DEFAULT_PRODUCT and is_planetary_computer_available()
    ) else []
    years = sorted(licensed_years | set(public_years))
    return {
        "available_years": years,
        "licensed_available_years": sorted(licensed_years),
        "public_available_years": public_years,
        "latest_available_year": max(years) if years else None,
        "licensed_product_year_range": list(CHLORIS_LICENSED_YEAR_RANGE),
        "configuration_note": configuration_note,
    }


def chloris_dataset_metadata(meta: dict, requested_year: int | None = None) -> dict:
    """Adapt the picker vintage/resolution to the source available on this server."""
    availability = chloris_availability()
    years = availability["available_years"]
    year = requested_year if requested_year is not None else availability["latest_available_year"]
    licensed = year in availability["licensed_available_years"]
    return {
        **meta,
        **availability,
        "year": year,
        "year_range": [min(years), max(years)] if years else None,
        "resolution": get_settings().chloris_stock_resolution_m if licensed else 4633,
        "source": "chloris_licensed" if licensed else "planetary_computer_stac",
    }


def _is_chloris_origin(url: str) -> bool:
    origin = urlsplit(get_settings().chloris_base_url)
    target = urlsplit(url)
    return target.scheme == "https" and (target.scheme, target.netloc) == (origin.scheme, origin.netloc)


def chloris_raster_http_options(url: str) -> dict[str, str]:
    """Keep Chloris bearer credentials on the configured Chloris HTTPS origin."""
    if _is_chloris_origin(url):
        token = _get_id_token()
        if token:
            return {
                "GDAL_HTTP_HEADERS": f"Authorization: Bearer {token}",
                "CPL_VSIL_CURL_AUTHORIZATION_HEADER_ALLOWED_IF_REDIRECT": "IF_SAME_HOST",
            }
    return {}


def resolve_chloris_download(product: str = DEFAULT_PRODUCT, year: int | None = None) -> ChlorisDownload:
    """
    Resolve a Chloris GeoTIFF download from a reporting unit downloads.json.

    Configuration paths, in priority order:
      1. CHLORIS_<PRODUCT>_URL or CHLORIS_AGB_STOCK_URL direct GeoTIFF URL.
      2. CHLORIS_DATA_PATH pointing to the reporting unit data folder.
      3. CHLORIS_ORGANIZATION_ID + CHLORIS_ID_TOKEN (+ optional reporting unit id)
         to fetch the reporting unit list and read its dataPath.
    """
    normalized_product = (product or DEFAULT_PRODUCT).strip().lower()
    dated = _direct_downloads(normalized_product)
    selected_year = int(year) if year is not None else max(dated, default=None)
    if selected_year is not None and selected_year in dated:
        return ChlorisDownload(
            url=_to_https_url(dated[selected_year]),
            product=normalized_product,
            date=str(selected_year),
            format="tif",
            metadata={"source": "direct_env_url", "year": selected_year},
        )

    # Prefer the licensed Chloris reporting-unit download when configured.
    # If it is not available, use the public annual collection hosted by
    # Microsoft's Planetary Computer. This keeps the dataset selectable on a
    # fresh install while preserving the higher-resolution licensed path.
    licensed_error: Exception | None = None
    try:
        if not _has_licensed_index_config():
            raise ChlorisUnavailableError("Sumber Chloris berlisensi belum dikonfigurasi.")
        downloads = _licensed_downloads_index()
        entry = _select_download(downloads, normalized_product, year)
        url = entry.get("url")
        if not url:
            raise ValueError(
                f"Chloris downloads.json tidak memiliki field url untuk product='{normalized_product}'."
            )

        return ChlorisDownload(
            url=_to_https_url(str(url)),
            product=normalized_product,
            date=str(entry.get("date")) if entry.get("date") is not None else None,
            format=str(entry.get("format")) if entry.get("format") is not None else None,
            metadata={**entry, "source": "chloris_reporting_unit", "year": _entry_year(entry)},
        )
    except Exception as exc:  # noqa: BLE001 - public STAC fallback is attempted below
        licensed_error = exc

    if normalized_product != DEFAULT_PRODUCT:
        raise ChlorisUnavailableError(
            f"Chloris product '{normalized_product}' belum tersedia untuk tahun yang diminta. "
            "Periksa reporting unit dan hak unduh raster Chloris."
        ) from licensed_error

    if year is not None and not PC_CHLORIS_YEAR_RANGE[0] <= int(year) <= PC_CHLORIS_YEAR_RANGE[1]:
        raise ChlorisUnavailableError(
            f"Chloris {year} belum tersedia di server ini. Tahun 2024/2025 memerlukan raster Chloris "
            "berlisensi: isi CHLORIS_STOCK_2024_URL/CHLORIS_STOCK_2025_URL, atau "
            "reporting unit dengan hak unduh GeoTIFF untuk tahun tersebut. "
            "Koleksi publik hanya mencakup 2003-2019."
        ) from licensed_error

    try:
        return _resolve_planetary_computer_download(year)
    except Exception as pc_error:  # noqa: BLE001 - include both setup failures in the API error
        raise ChlorisUnavailableError(
            "Chloris licensed download tidak dapat diakses dan fallback Planetary Computer juga gagal. "
            "Periksa konfigurasi akses dan koneksi ke penyedia data."
        ) from pc_error


def is_planetary_computer_available() -> bool:
    """Return whether the public Chloris STAC fallback can be attempted."""
    try:
        import planetary_computer  # noqa: F401
        import pystac_client  # noqa: F401
    except ImportError:
        return False
    return True


def _resolve_planetary_computer_download(year: int | None = None) -> ChlorisDownload:
    """Resolve a signed annual Chloris biomass COG from Planetary Computer."""
    if not is_planetary_computer_available():
        raise ValueError("Dependency pystac-client/planetary-computer belum terpasang.")

    from pystac_client import Client
    import planetary_computer

    requested_year = int(year) if year is not None else PC_CHLORIS_YEAR_RANGE[1]
    if requested_year < PC_CHLORIS_YEAR_RANGE[0] or requested_year > PC_CHLORIS_YEAR_RANGE[1]:
        raise ValueError(
            f"Planetary Computer Chloris hanya menyediakan tahun {PC_CHLORIS_YEAR_RANGE[0]}-"
            f"{PC_CHLORIS_YEAR_RANGE[1]}; tahun yang diminta: {requested_year}."
        )

    client = Client.open(PC_STAC_URL)
    start = f"{requested_year}-01-01T00:00:00Z"
    end = f"{requested_year}-12-31T23:59:59Z"
    items = list(client.search(
        collections=[PC_CHLORIS_COLLECTION],
        datetime=f"{start}/{end}",
    ).items())
    if not items:
        raise ValueError(f"Tidak ada item STAC Chloris untuk tahun {requested_year}.")

    item = sorted(items, key=lambda candidate: str(candidate.datetime or ""), reverse=True)[0]
    asset = item.assets.get("biomass") or item.assets.get("biomass_wm")
    if asset is None:
        raise ValueError(f"Item STAC Chloris '{item.id}' tidak memiliki asset biomass.")

    signed_asset = planetary_computer.sign(asset)
    item_year = item.datetime.year if item.datetime else requested_year
    return ChlorisDownload(
        url=signed_asset.href,
        product=DEFAULT_PRODUCT,
        date=str(item_year),
        format="cog",
        metadata={
            "source": "planetary_computer_stac",
            "collection": PC_CHLORIS_COLLECTION,
            "item_id": item.id,
            "year": item_year,
            "asset": "biomass",
            "license": "CC-BY-NC-SA-4.0",
        },
    )


@lru_cache(maxsize=20)
def _planetary_computer_chloris_item(year: int) -> tuple[str, float]:
    """Return the STAC item id and equal-area pixel area (ha) for one year."""
    requested_year = int(year)
    if requested_year < PC_CHLORIS_YEAR_RANGE[0] or requested_year > PC_CHLORIS_YEAR_RANGE[1]:
        raise ValueError(
            f"Planetary Computer Chloris hanya menyediakan tahun {PC_CHLORIS_YEAR_RANGE[0]}-"
            f"{PC_CHLORIS_YEAR_RANGE[1]}; tahun yang diminta: {requested_year}."
        )

    response = requests.post(
        f"{PC_STAC_URL}/search",
        json={
            "collections": [PC_CHLORIS_COLLECTION],
            "datetime": f"{requested_year}-01-01T00:00:00Z/{requested_year}-12-31T23:59:59Z",
            "limit": 10,
        },
        timeout=(5, 20),
    )
    response.raise_for_status()
    items = response.json().get("features", [])
    if not items:
        raise ValueError(f"Tidak ada item STAC Chloris untuk tahun {requested_year}.")

    item = sorted(
        items,
        key=lambda candidate: str(candidate.get("properties", {}).get("datetime") or ""),
        reverse=True,
    )[0]
    item_id = item.get("id")
    properties = item.get("properties") or {}
    if not item_id or "biomass" not in (item.get("assets") or {}):
        raise ValueError(f"Item STAC Chloris tahun {requested_year} tidak memiliki asset biomass.")

    transform = properties.get("proj:transform")
    if isinstance(transform, list) and len(transform) >= 6:
        pixel_area_m2 = abs(
            float(transform[0]) * float(transform[4])
            - float(transform[1]) * float(transform[3])
        )
    else:
        pixel_size = float(properties.get("gsd") or 0)
        pixel_area_m2 = pixel_size**2
    if pixel_area_m2 <= 0:
        raise ValueError(f"Item STAC Chloris '{item_id}' tidak memiliki resolusi raster yang valid.")

    return str(item_id), pixel_area_m2 / 10_000


def resolve_planetary_computer_tile_url(
    year: int,
    vis_min: float,
    vis_max: float,
    carbon_fraction: float = CHLORIS_BIOMASS_TO_CARBON_FRACTION,
) -> str:
    """Build a public PC tile URL scaled to the app's Mg C/ha visualization."""
    if vis_max <= vis_min:
        raise ValueError("Nilai visualisasi maksimum harus lebih besar dari minimum.")
    item_id, pixel_area_ha = _planetary_computer_chloris_item(int(year))
    tonnes_per_pixel_min = vis_min * pixel_area_ha / carbon_fraction
    tonnes_per_pixel_max = vis_max * pixel_area_ha / carbon_fraction
    query = urlencode(
        {
            "collection": PC_CHLORIS_COLLECTION,
            "item": item_id,
            "assets": "biomass",
            "colormap_name": "chloris-biomass",
            "rescale": f"{tonnes_per_pixel_min:g},{tonnes_per_pixel_max:g}",
            "format": "png",
        }
    )
    return f"{PC_DATA_API_URL}/item/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}@1x?{query}"


def _get_data_path() -> str:
    settings = get_settings()
    if settings.chloris_data_path:
        return _to_https_url(settings.chloris_data_path).rstrip("/")

    reporting_unit = _get_reporting_unit()
    data_path = reporting_unit.get("dataPath")
    if not data_path:
        raise ValueError(
            "Chloris dataPath belum tersedia. Isi CHLORIS_DATA_PATH, atau isi "
            "CHLORIS_ORGANIZATION_ID + CHLORIS_ID_TOKEN agar backend bisa membaca reporting unit."
        )
    return _to_https_url(str(data_path)).rstrip("/")


def _get_reporting_unit() -> dict[str, Any]:
    settings = get_settings()
    if not settings.chloris_organization_id:
        raise ValueError("CHLORIS_ORGANIZATION_ID belum diisi.")
    if not settings.chloris_id_token and not settings.chloris_refresh_token:
        raise ValueError(
            "CHLORIS_ID_TOKEN atau CHLORIS_REFRESH_TOKEN belum diisi. Ambil API credentials "
            "dari profile Chloris, lalu set token server-side di .env."
        )

    url = _join_url(settings.chloris_base_url, "api/reportingUnit/")
    payload: dict[str, Any] = {"organizationId": settings.chloris_organization_id}
    if settings.chloris_reporting_unit_id:
        payload["reportingUnitId"] = settings.chloris_reporting_unit_id
    response = requests.post(url, json=payload, headers=_auth_headers(), timeout=45)
    response.raise_for_status()
    data = response.json()

    candidates = _extract_reporting_units(data)
    if not candidates:
        raise ValueError("Chloris API tidak mengembalikan reporting unit.")

    wanted = settings.chloris_reporting_unit_id
    if wanted:
        for item in candidates:
            if wanted in {str(item.get("id")), str(item.get("reportingUnitId")), str(item.get("uuid"))}:
                return item
        raise ValueError(f"Reporting unit Chloris '{wanted}' tidak ditemukan untuk organisasi ini.")

    return candidates[0]


def _extract_reporting_units(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in ("reportingUnits", "reporting_units", "items", "results", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    if data.get("dataPath"):
        return [data]
    reporting_unit = data.get("reportingUnit")
    if isinstance(reporting_unit, dict):
        return [reporting_unit]
    return []


def _select_download(downloads: Any, product: str, year: int | None) -> dict[str, Any]:
    entries = _extract_download_entries(downloads)
    tif_entries = [
        entry
        for entry in entries
        if _entry_product(entry) == product and entry.get("url")
        and _entry_format(entry) in {"tif", "tiff", "geotiff", "cog", ""}
    ]
    if not tif_entries:
        raise ValueError(f"Chloris downloads.json tidak memiliki GeoTIFF untuk product='{product}'.")

    if year is not None:
        exact = [entry for entry in tif_entries if _entry_year(entry) == int(year)]
        if exact:
            return _latest_by_date(exact)
        available = sorted({value for entry in tif_entries if (value := _entry_year(entry)) is not None})
        raise ChlorisUnavailableError(
            f"Chloris '{product}' tahun {year} tidak tersedia pada reporting unit. "
            f"Tahun tersedia: {', '.join(map(str, available))}."
        )

    return _latest_by_date(tif_entries)


def _extract_download_entries(downloads: Any) -> list[dict[str, Any]]:
    if isinstance(downloads, list):
        return [entry for entry in downloads if isinstance(entry, dict)]
    if isinstance(downloads, dict):
        for key in ("downloads", "items", "results", "data"):
            value = downloads.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
        if "url" in downloads:
            return [downloads]
    return []


def _entry_product(entry: dict[str, Any]) -> str:
    for key in ("urlFormat", "product", "productType", "url_format"):
        value = entry.get(key)
        if value:
            return str(value).strip().lower()
    return ""


def _entry_format(entry: dict[str, Any]) -> str:
    value = entry.get("format") or entry.get("fileFormat")
    return str(value).strip().lower() if value else ""


def _entry_year(entry: dict[str, Any]) -> int | None:
    date = entry.get("date") or entry.get("year")
    if date is None:
        return None
    text = str(date)
    try:
        return int(text[:4])
    except ValueError:
        return None


def _latest_by_date(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return max(entries, key=lambda item: (_entry_year(item) or 0, str(item.get("date") or "")))


def _to_https_url(url_or_path: str) -> str:
    settings = get_settings()
    value = url_or_path.strip()
    if value.startswith(CHLORIS_S3_PREFIX):
        return _join_url(settings.chloris_base_url, value.removeprefix(CHLORIS_S3_PREFIX))
    return value


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _auth_headers() -> dict[str, str]:
    token = _get_id_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _request_json(url: str) -> Any:
    response = requests.get(url, headers=_auth_headers() if _is_chloris_origin(url) else {}, timeout=45)
    response.raise_for_status()
    return response.json()


def _get_id_token() -> str:
    settings = get_settings()
    if settings.chloris_id_token:
        return settings.chloris_id_token
    if settings.chloris_refresh_token:
        return _refresh_id_token(settings.chloris_refresh_token, int(time.monotonic() // 1800))
    return ""


@lru_cache(maxsize=8)
def _refresh_id_token(refresh_token: str, _time_bucket: int = 0) -> str:
    settings = get_settings()
    api_info = _request_public_json(_join_url(settings.chloris_base_url, "api/info"))
    region = api_info.get("awsRegion")
    client_id = api_info.get("awsUserPoolWebClientId")
    if not region or not client_id:
        raise ValueError("Chloris /api/info tidak mengembalikan Cognito region/client id.")

    endpoint = f"https://cognito-idp.{region}.amazonaws.com/"
    payload = {
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": client_id,
        "AuthParameters": {"REFRESH_TOKEN": refresh_token},
    }
    response = requests.post(
        endpoint,
        json=payload,
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()
    id_token = data.get("AuthenticationResult", {}).get("IdToken")
    if not id_token:
        raise ValueError("Refresh token Chloris tidak menghasilkan IdToken.")
    return str(id_token)


def _request_public_json(url: str) -> Any:
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def _has_direct_download_url() -> bool:
    settings = get_settings()
    return bool(
        settings.chloris_agb_stock_url or settings.chloris_stock_url
        or settings.chloris_stock_url_template or settings.chloris_stock_2024_url
        or settings.chloris_stock_2025_url or any(
            value for key, value in os.environ.items()
            if re.fullmatch(r"CHLORIS_(?:AGB_)?STOCK_\d{4}_URL", key)
        )
    )
