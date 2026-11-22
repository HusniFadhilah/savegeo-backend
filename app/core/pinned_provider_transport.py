"""Pinned transports for provider SDKs that expose an injectable HTTP client."""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import httpx2

from app.core.public_http import public_http


_SDK_HEADERS = {
    "accept", "authorization", "content-type", "user-agent", "x-api-key", "x-goog-api-key",
    "x-goog-user-project", "x-goog-api-client", "x-goog-request-params", "x-goog-quota-user",
    "x-goog-upload-protocol", "x-goog-upload-command", "x-goog-upload-offset",
    "x-upload-content-type", "x-upload-content-length", "anthropic-version", "anthropic-beta",
    "openai-organization", "openai-project", "idempotency-key", "http-referer", "x-title",
    "x-stainless-lang", "x-stainless-package-version", "x-stainless-os", "x-stainless-arch",
    "x-stainless-runtime", "x-stainless-runtime-version",
}


class BoundedPublicHTTPTransport(httpx.BaseTransport):
    """Route one configured public provider host through the pinned HTTPS client."""

    def __init__(self, expected_hostname: str, *, timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024,
                 max_request_bytes: int = 2 * 1024 * 1024) -> None:
        host = expected_hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or len(host) > 253:
            raise ValueError("Invalid provider host")
        self.expected_hostname = host
        self.timeout = min(max(float(timeout), 1.0), 60.0)
        self.max_response_bytes = min(max(int(max_response_bytes), 1024), 16 * 1024 * 1024)
        self.max_request_bytes = min(max(int(max_request_bytes), 1024), 16 * 1024 * 1024)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            parsed = urlsplit(str(request.url))
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".") != self.expected_hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in {None, 443}
                or parsed.fragment
            ):
                raise ValueError("Provider SDK request destination is not allowed")

            body = request.read()
            if len(body) > self.max_request_bytes:
                raise ValueError("Provider SDK request exceeds the size limit")
            headers = {
                name.lower(): value
                for name, value in request.headers.items()
                if name.lower() in _SDK_HEADERS
            }
            upstream = public_http.request(
                request.method,
                str(request.url),
                raw_body=body or None,
                headers=headers,
                timeout=self.timeout,
                max_bytes=self.max_response_bytes,
                max_request_bytes=self.max_request_bytes,
            )
            return httpx.Response(
                status_code=upstream.status_code,
                headers=dict(upstream.headers),
                content=upstream.content,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001 - do not surface signed URLs or provider credentials
            if isinstance(exc, httpx.RequestError):
                raise
            raise httpx.RequestError("Bounded provider HTTPS request failed", request=request) from exc


class BoundedPublicHTTPX2Transport(httpx2.BaseTransport):
    """Same public-host policy for SDKs that use the separately versioned httpx2."""

    def __init__(self, expected_hostname: str, *, timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024,
                 max_request_bytes: int = 2 * 1024 * 1024) -> None:
        host = expected_hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or len(host) > 253:
            raise ValueError("Invalid provider host")
        self.expected_hostname = host
        self.timeout = min(max(float(timeout), 1.0), 60.0)
        self.max_response_bytes = min(max(int(max_response_bytes), 1024), 16 * 1024 * 1024)
        self.max_request_bytes = min(max(int(max_request_bytes), 1024), 16 * 1024 * 1024)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            upstream = _perform_public_provider_request(
                request, self.expected_hostname, timeout=self.timeout,
                max_response_bytes=self.max_response_bytes, max_request_bytes=self.max_request_bytes,
            )
            return httpx2.Response(
                status_code=upstream.status_code,
                headers=dict(upstream.headers),
                content=upstream.content,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001 - do not surface signed URLs or provider credentials
            if isinstance(exc, httpx2.RequestError):
                raise
            raise httpx2.RequestError("Bounded provider HTTPS request failed", request=request) from exc


def _perform_public_provider_request(request, expected_hostname: str, *, timeout: float,
                                    max_response_bytes: int, max_request_bytes: int):
    parsed = urlsplit(str(request.url))
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".") != expected_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("Provider SDK request destination is not allowed")
    body = request.read()
    if len(body) > max_request_bytes:
        raise ValueError("Provider SDK request exceeds the size limit")
    headers = {
        name.lower(): value
        for name, value in request.headers.items()
        if name.lower() in _SDK_HEADERS
    }
    return public_http.request(
        request.method,
        str(request.url),
        raw_body=body or None,
        headers=headers,
        timeout=timeout,
        max_bytes=max_response_bytes,
        max_request_bytes=max_request_bytes,
    )


class BoundedEarthEngineTransport:
    """httplib2-compatible transport that pins Earth Engine and Google token requests."""

    ALLOWED_HOSTS = frozenset({
        "earthengine.googleapis.com",
        "earthengine-highvolume.googleapis.com",
        "oauth2.googleapis.com",
        "www.googleapis.com",
    })
    MAX_RESPONSE_BYTES = 64 * 1024 * 1024
    MAX_REQUEST_BYTES = 16 * 1024 * 1024

    def __init__(self, *, timeout: float = 60) -> None:
        self.timeout = min(max(float(timeout), 1.0), 60.0)

    def request(self, uri: str, method: str = "GET", body=None, headers=None,
                redirections=None, connection_type=None):
        del redirections, connection_type
        import httplib2

        try:
            parsed = urlsplit(uri)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            if (
                parsed.scheme != "https"
                or host not in self.ALLOWED_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in {None, 443}
                or parsed.fragment
            ):
                raise ValueError("Earth Engine request destination is not allowed")
            safe_headers = {
                str(name).lower(): str(value)
                for name, value in (headers or {}).items()
                if str(name).lower() in _SDK_HEADERS
            }
            upstream = public_http.request(
                method,
                uri,
                raw_body=body if body else None,
                headers=safe_headers,
                timeout=self.timeout,
                max_bytes=self.MAX_RESPONSE_BYTES,
                max_request_bytes=self.MAX_REQUEST_BYTES,
            )
            response_headers = {str(name).lower(): value for name, value in upstream.headers.items()}
            response_headers["status"] = str(upstream.status_code)
            return httplib2.Response(response_headers), upstream.content
        except Exception as exc:  # noqa: BLE001 - avoid logging provider payloads or credentials
            if isinstance(exc, httplib2.HttpLib2Error):
                raise
            raise httplib2.HttpLib2Error("Bounded Earth Engine HTTPS request failed") from exc


def public_httpx_client(base_url: str, *, timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024):
    """Create an SDK client whose requests stay on its configured HTTPS host."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Cloud provider base URL must be public HTTPS without credentials or query parameters")
    transport = BoundedPublicHTTPTransport(
        parsed.hostname, timeout=timeout, max_response_bytes=max_response_bytes,
    )
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=5),
    )


def public_httpx2_client(base_url: str, *, timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024):
    """Create the equivalent bounded client for SDKs pinned to httpx2."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Cloud provider base URL must be public HTTPS without credentials or query parameters")
    transport = BoundedPublicHTTPX2Transport(
        parsed.hostname, timeout=timeout, max_response_bytes=max_response_bytes,
    )
    return httpx2.Client(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx2.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=5),
    )
