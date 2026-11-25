"""Read-only adapter for an officially documented carbon-registry API.

The registry endpoint and credential are deployment configuration, never
project-supplied URLs. The response contract is normalized to a JSON object
with a ``rows`` array matching RegistryExternalRow. SaveGeo performs no remote
write operation from this adapter.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

import requests

from app.schemas.dmrv_carbon_features import RegistryReconciliationInput
from app.services.dmrv.errors import DmrvError

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SUPPORTED_PROVIDERS = {"verra_gateway", "unfccc_article_6_4"}


def _settings() -> tuple[str, str, str, set[str]]:
    endpoint = os.getenv("SAVEGEO_REGISTRY_READONLY_ENDPOINT", "").strip()
    token = os.getenv("SAVEGEO_REGISTRY_READONLY_TOKEN", "").strip()
    provider = os.getenv("SAVEGEO_REGISTRY_PROVIDER", "").strip().lower()
    hosts = {item.strip().lower().rstrip(".") for item in os.getenv("SAVEGEO_REGISTRY_ALLOWED_HOSTS", "").split(",") if item.strip()}
    return endpoint, token, provider, hosts


def status() -> dict:
    endpoint, token, provider, hosts = _settings()
    parsed = urlparse(endpoint)
    configured = bool(
        endpoint and token and provider in SUPPORTED_PROVIDERS and parsed.scheme == "https"
        and parsed.hostname and parsed.hostname.lower().rstrip(".") in hosts
    )
    return {
        "configured": configured,
        "provider": provider or None,
        "read_only": True,
        "write_operations": False,
        "status": "ready" if configured else "official_api_configuration_required",
        "required_environment": [
            "SAVEGEO_REGISTRY_PROVIDER", "SAVEGEO_REGISTRY_READONLY_ENDPOINT",
            "SAVEGEO_REGISTRY_READONLY_TOKEN", "SAVEGEO_REGISTRY_ALLOWED_HOSTS",
        ],
        "response_contract": "JSON object {rows:[{external_project_id,vintage,serial,event_type,event_date,quantity,account,transaction_id}],source_url?}",
        "official_docs": {
            "verra": "https://verra.org/verra-project-hub/",
            "unfccc_article_6_4": "https://unfccc.int/process-and-meetings/the-paris-agreement/article-6/article-64-pacm/registry",
        },
        "message": "SaveGeo only calls a server-configured, allow-listed HTTPS endpoint after the organization enables official registry API access.",
    }


def _public_host(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


def fetch(registry: str, external_project_id: str) -> RegistryReconciliationInput:
    endpoint, token, provider, hosts = _settings()
    configured = status()["configured"]
    if not configured:
        raise DmrvError(503, "registry_connector_not_configured", "Konfigurasi API resmi registry belum lengkap di server.")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username or parsed.password or parsed.port not in {None, 443} or host not in hosts or not _public_host(host):
        raise DmrvError(503, "registry_endpoint_rejected", "Endpoint registry harus HTTPS, host publik, port 443, dan ada di allowlist deployment.")
    if registry.casefold() not in {"verra registry", "unfccc article 6.4 registry"}:
        raise DmrvError(422, "unsupported_registry", "Konektor resmi hanya mengizinkan Verra Registry atau UNFCCC Article 6.4.")
    if (provider == "verra_gateway") != (registry.casefold() == "verra registry"):
        raise DmrvError(422, "registry_provider_mismatch", "Nama registry tidak sesuai konfigurasi konektor server.")
    try:
        response = requests.get(
            endpoint,
            params={"external_project_id": external_project_id},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=(4, 20), allow_redirects=False, stream=True,
        )
        if response.is_redirect:
            raise DmrvError(502, "registry_redirect_blocked", "Endpoint registry mengalihkan request; redirect diblokir demi keamanan.")
        if response.status_code >= 400:
            raise DmrvError(502, "registry_api_error", f"API registry mengembalikan HTTP {response.status_code}.")
        length = int(response.headers.get("Content-Length", "0") or 0)
        if length > MAX_RESPONSE_BYTES:
            raise DmrvError(502, "registry_response_too_large", "Respons registry melebihi 5 MB.")
        chunks = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise DmrvError(502, "registry_response_too_large", "Respons registry melebihi 5 MB.")
            chunks.append(chunk)
        body = json.loads(b"".join(chunks))
    except DmrvError:
        raise
    except (requests.RequestException, ValueError, TypeError):
        raise DmrvError(502, "registry_api_unavailable", "Koneksi/API registry tidak tersedia atau mengembalikan JSON tidak valid.") from None
    if not isinstance(body, dict) or not isinstance(body.get("rows"), list) or not body["rows"] or len(body["rows"]) > 1000:
        raise DmrvError(502, "registry_response_contract", "Respons API harus memiliki 1–1.000 baris pada properti rows.")
    if any(str(row.get("external_project_id", "")) != external_project_id for row in body["rows"] if isinstance(row, dict)):
        raise DmrvError(502, "registry_project_mismatch", "API mengembalikan baris untuk ID proyek yang berbeda.")
    source_url = body.get("source_url") or endpoint
    source = urlparse(str(source_url))
    if source.scheme != "https" or not source.hostname or source.username or source.password or source.hostname.lower().rstrip(".") not in hosts:
        raise DmrvError(502, "registry_source_url_rejected", "Tautan sumber respons harus HTTPS dan host-nya termasuk allowlist.")
    try:
        return RegistryReconciliationInput.model_validate({
            "registry": registry,
            "source_url": source_url,
            "rows": body["rows"],
            "evidence": [source_url],
        })
    except Exception:
        raise DmrvError(502, "registry_response_contract", "Baris registry tidak sesuai kontrak data eksternal yang diharapkan SaveGeo.") from None
