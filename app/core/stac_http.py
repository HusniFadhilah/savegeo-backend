"""Bounded STAC API and Planetary Computer signing transport."""
from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from pystac.link import Link
from pystac_client.stac_api_io import StacApiIO

from app.core.public_http import public_http


class BoundedStacApiIO(StacApiIO):
    """STAC IO that pins HTTPS destinations and bounds pages and responses."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str],
        allowed_suffixes: set[str] | None = None,
        timeout: float = 20,
        max_bytes: int = 8 * 1024 * 1024,
        max_pages: int = 20,
    ) -> None:
        if not allowed_hosts and not allowed_suffixes:
            raise ValueError("At least one STAC host allowlist entry is required")
        super().__init__(timeout=timeout, max_retries=None)
        self.allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts}
        self.allowed_suffixes = {host.lower().lstrip(".").rstrip(".") for host in (allowed_suffixes or set())}
        self.max_bytes = max(1024, int(max_bytes))
        self.max_pages = max(1, min(int(max_pages), 100))

    def _validate_host(self, href: str) -> None:
        parsed = urlsplit(href)
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not hostname
            or not (
                hostname in self.allowed_hosts
                or any(hostname == suffix or hostname.endswith("." + suffix) for suffix in self.allowed_suffixes)
            )
        ):
            raise ValueError("STAC request destination is not allowed")
        public_http.validate_public_destination(href)

    def request(
        self,
        href: str,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        parameters: dict | None = None,
    ) -> str:
        method = (method or "GET").upper()
        self._validate_host(href)
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() in {"accept", "content-type", "user-agent"}
        }
        if method == "GET":
            response = public_http.get(
                href,
                params=parameters,
                headers=safe_headers,
                timeout=self.timeout or 20,
                max_bytes=self.max_bytes,
            )
        elif method == "POST":
            response = public_http.post(
                href,
                json_body=parameters or {},
                headers=safe_headers,
                timeout=self.timeout or 20,
                max_bytes=self.max_bytes,
            )
        else:
            raise ValueError("STAC only supports GET and POST")
        response.raise_for_status()
        # Round-trip JSON to reject non-JSON and non-object response bodies
        # before PySTAC tries to deserialize untrusted provider data.
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("STAC provider returned a non-object response")
        return json.dumps(payload, allow_nan=False, separators=(",", ":"))

    def get_pages(self, url: str, method: str | None = None, parameters: dict | None = None):
        page = self.read_json(url, method=method, parameters=parameters)
        for page_index in range(self.max_pages):
            if not (page.get("features") or page.get("collections")):
                return
            yield page
            next_link = next((entry for entry in page.get("links", []) if entry.get("rel") == "next"), None)
            if not next_link:
                return
            if page_index + 1 >= self.max_pages:
                return
            # The next link is revalidated by request() before it is fetched.
            page = self.read_json(Link.from_dict(next_link), parameters=parameters)


def sign_planetary_computer_href(href: str) -> str:
    """Sign an Azure asset using the bounded public transport, without SDK HTTP."""
    from planetary_computer.sas import SASToken, TOKEN_CACHE, parse_blob_url
    from planetary_computer.settings import Settings

    parsed = urlsplit(href)
    hostname = (parsed.hostname or "").lower()
    if hostname == "ai4edatasetspublicassets.blob.core.windows.net":
        return href
    if not hostname.endswith(".blob.core.windows.net"):
        return href
    if parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password:
        raise ValueError("Planetary Computer asset URL is invalid")
    query_keys = {part.split("=", 1)[0].lower() for part in parsed.query.split("&") if part}
    if query_keys.intersection({"st", "se", "sp"}):
        return href

    account, container = parse_blob_url(parsed)
    settings = Settings.get()
    sas_base = (settings.sas_url or "").rstrip("/")
    sas_parsed = urlsplit(sas_base)
    if (
        sas_parsed.scheme != "https"
        or sas_parsed.hostname != "planetarycomputer.microsoft.com"
        or sas_parsed.port not in {None, 443}
    ):
        raise ValueError("Planetary Computer signing endpoint is not allowed")
    token_url = f"{sas_base}/{quote(account, safe='')}/{quote(container, safe='')}"
    token = TOKEN_CACHE.get(token_url)
    if token is None or token.ttl() < 60:
        headers = {"Ocp-Apim-Subscription-Key": settings.subscription_key} if settings.subscription_key else {}
        response = public_http.get(token_url, headers=headers, timeout=10, max_bytes=64 * 1024)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Planetary Computer signing response is invalid")
        token = SASToken(**payload)
        TOKEN_CACHE[token_url] = token
    return token.sign(href).href
