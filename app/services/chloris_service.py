"""Chloris API/download helpers for carbon stock reference rasters."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CHLORIS_S3_PREFIX = "s3://chloris-app-data/"
DEFAULT_PRODUCT = "stock"


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
    return bool(has_direct_path or has_api_path)


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
    }


def resolve_chloris_download(product: str = DEFAULT_PRODUCT, year: int | None = None) -> ChlorisDownload:
    """
    Resolve a Chloris GeoTIFF download from a reporting unit downloads.json.

    Configuration paths, in priority order:
      1. CHLORIS_<PRODUCT>_URL or CHLORIS_AGB_STOCK_URL direct GeoTIFF URL.
      2. CHLORIS_DATA_PATH pointing to the reporting unit data folder.
      3. CHLORIS_ORGANIZATION_ID + CHLORIS_ID_TOKEN (+ optional reporting unit id)
         to fetch the reporting unit list and read its dataPath.
    """
    import os

    normalized_product = (product or DEFAULT_PRODUCT).strip().lower()
    direct_url = (
        os.getenv(f"CHLORIS_{normalized_product.upper()}_URL")
        or os.getenv("CHLORIS_AGB_STOCK_URL")
    )
    if direct_url:
        return ChlorisDownload(
            url=_to_https_url(direct_url),
            product=normalized_product,
            date=str(year) if year else None,
            format="tif",
            metadata={"source": "direct_env_url"},
        )

    data_path = _get_data_path()
    downloads_url = _join_url(data_path, "downloads.json")
    downloads = _request_json(downloads_url)
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
        metadata=entry,
    )


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
        if _entry_product(entry) == product and _entry_format(entry) in {"tif", "tiff", "geotiff", ""}
    ]
    if not tif_entries:
        raise ValueError(f"Chloris downloads.json tidak memiliki GeoTIFF untuk product='{product}'.")

    if year is not None:
        exact = [entry for entry in tif_entries if _entry_year(entry) == int(year)]
        if exact:
            return _latest_by_date(exact)

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
    return sorted(entries, key=lambda item: str(item.get("date") or ""), reverse=True)[0]


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
    response = requests.get(url, headers=_auth_headers(), timeout=45)
    response.raise_for_status()
    return response.json()


def _get_id_token() -> str:
    settings = get_settings()
    if settings.chloris_id_token:
        return settings.chloris_id_token
    if settings.chloris_refresh_token:
        return _refresh_id_token(settings.chloris_refresh_token)
    return ""


def _refresh_id_token(refresh_token: str) -> str:
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
    import os

    return bool(os.getenv("CHLORIS_AGB_STOCK_URL") or os.getenv("CHLORIS_STOCK_URL"))
