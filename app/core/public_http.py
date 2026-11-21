"""Bounded HTTPS transport for public, untrusted destinations.

Resolve once, reject every non-public answer and connect to that exact address.
TLS still verifies the original hostname and sends it as SNI. This transport
does not inherit proxy credentials, netrc or redirect behavior from the host.
Operator-configured internal services (e.g. Ollama) do not use this client.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode, urlsplit, urlunsplit

import certifi
import requests
import urllib3

_CIRCUIT_LOCK = threading.Lock()
_CIRCUITS: dict[str, dict[str, float | int | bool]] = {}
_CIRCUIT_FAILURE_THRESHOLD = 5
_CIRCUIT_OPEN_SECONDS = 30.0


def public_addresses(host: str) -> list[str]:
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(item[4][0]) for item in answers))
    if not addresses or len(addresses) > 16:
        raise ValueError("Invalid public DNS response")
    for address in addresses:
        value = ipaddress.ip_address(address)
        mapped = getattr(value, "ipv4_mapped", None)
        if not value.is_global or (mapped is not None and not mapped.is_global):
            raise ValueError("Destination is not a public address")
    return addresses


class BoundedStreamingResponse:
    """Small response wrapper that streams from one already-pinned socket."""

    def __init__(self, upstream, pool, *, url: str, hostname: str, deadline: float, max_bytes: int):
        self.status_code = int(upstream.status)
        self.headers = upstream.headers
        self.url = url
        self._upstream = upstream
        self._pool = pool
        self._hostname = hostname
        self._deadline = deadline
        self._max_bytes = max_bytes
        self._closed = False

    def iter_bytes(self, chunk_size: int = 64 * 1024):
        raw = getattr(self._upstream, "_fp", None)
        read_chunk = getattr(raw, "read1", None)
        if not callable(read_chunk):
            self.close(success=False)
            raise requests.RequestException("Streaming response does not support bounded reads")
        total = 0
        completed = False
        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout("Public HTTPS stream deadline exceeded")
                chunk = read_chunk(min(chunk_size, 256 * 1024))
                if not chunk:
                    completed = True
                    break
                total += len(chunk)
                if total > self._max_bytes:
                    raise ValueError("Public HTTPS stream exceeds the size limit")
                yield chunk
            self.close(success=completed and self.status_code < 500 and self.status_code != 429)
        except urllib3.exceptions.TimeoutError as exc:
            self.close(success=False)
            raise requests.Timeout("Public HTTPS stream timed out") from exc
        except urllib3.exceptions.HTTPError as exc:
            self.close(success=False)
            raise requests.RequestException("Public HTTPS stream failed") from exc
        except Exception:
            self.close(success=False)
            raise
        finally:
            self.close(success=completed and self.status_code < 500 and self.status_code != 429)

    def close(self, *, success: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._upstream.close()
        finally:
            self._pool.close()
            public_http._circuit_result(self._hostname, success=success)


class PublicHTTPS:
    @staticmethod
    def _validated_url(url: str):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}
                or any(ord(c) < 33 for c in url) or "\\" in url):
            raise ValueError("Only public HTTPS destinations are allowed")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        return parsed, hostname

    def validate_public_destination(self, url: str):
        """Validate HTTPS syntax and resolve every address as globally routable.

        Rasterio/GDAL cannot use the pinned socket in this module, so callers
        use this as a preflight guard and must also keep their source URL
        restricted to trusted provider catalogs.
        """
        parsed, hostname = self._validated_url(url)
        addresses = public_addresses(hostname)
        # Keep this check in the policy boundary itself as well as in the
        # resolver helper. Callers and tests may replace DNS resolution, and
        # a malformed resolver result must never authorize a private target.
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("Destination is not a public address")
        return parsed, hostname, addresses

    @contextmanager
    def provider_guard(self, hostname: str):
        """Apply the shared provider circuit breaker around non-HTTP clients.

        GDAL/libcurl cannot use this module's pinned socket, but its remote
        reads can still share the same per-host failure policy after a caller
        has validated the source URL. This is a resilience control, not a
        substitute for network egress filtering or DNS pinning.
        """
        if not isinstance(hostname, str) or not hostname or any(char in hostname for char in "/\\:@"):
            raise ValueError("Invalid provider hostname")
        host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or len(host) > 253:
            raise ValueError("Invalid provider hostname")
        self._circuit_before(host)
        try:
            public_addresses(host)
            yield
        except BaseException:
            self._circuit_result(host, success=False)
            raise
        else:
            self._circuit_result(host, success=True)

    @staticmethod
    def _timeout_seconds(timeout, *, maximum: float = 60.0) -> float:
        timeout_values = timeout if isinstance(timeout, (tuple, list)) else (timeout,)
        values = [float(value) for value in timeout_values]
        if not values or not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Invalid public HTTPS timeout")
        return min(max(values), maximum)

    @staticmethod
    def _circuit_before(hostname: str) -> None:
        now = time.monotonic()
        with _CIRCUIT_LOCK:
            state = _CIRCUITS.get(hostname)
            if not state:
                return
            opened_until = float(state.get("opened_until", 0))
            if opened_until > now:
                raise requests.ConnectionError("Provider circuit is open")
            if opened_until and bool(state.get("probe_in_flight")):
                raise requests.ConnectionError("Provider recovery probe is already running")
            if opened_until:
                state["probe_in_flight"] = True

    @staticmethod
    def _circuit_result(hostname: str, *, success: bool) -> None:
        with _CIRCUIT_LOCK:
            if success:
                _CIRCUITS.pop(hostname, None)
                return
            state = _CIRCUITS.setdefault(hostname, {"failures": 0, "opened_until": 0.0, "probe_in_flight": False})
            failures = int(state.get("failures", 0)) + 1
            state["failures"] = failures
            if failures >= _CIRCUIT_FAILURE_THRESHOLD or bool(state.get("probe_in_flight")):
                state["opened_until"] = time.monotonic() + _CIRCUIT_OPEN_SECONDS
            state["probe_in_flight"] = False

    def request(self, method: str, url: str, *, params=None, data=None, json_body=None, timeout=30,
                allow_redirects=False, allow_redirect_status=False,
                max_bytes=16 * 1024 * 1024, headers=None) -> requests.Response:
        _, hostname = self._validated_url(url)
        seconds = self._timeout_seconds(timeout)
        deadline = time.monotonic() + seconds
        method = method.upper()
        attempts = 2 if method in {"GET", "HEAD"} else 1
        self._circuit_before(hostname)
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = requests.Timeout("Public HTTPS deadline exceeded")
                self._circuit_result(hostname, success=False)
                raise error
            try:
                response = self._request_once(
                    method,
                    url,
                    params=params,
                    data=data,
                    json_body=json_body,
                    timeout=remaining,
                    allow_redirects=allow_redirects,
                    allow_redirect_status=allow_redirect_status,
                    max_bytes=max_bytes,
                    headers=headers,
                )
            except (requests.RequestException, OSError):
                self._circuit_result(hostname, success=False)
                if attempt + 1 >= attempts or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.15 * (2**attempt), max(0.0, deadline - time.monotonic())))
                continue
            if response.status_code == 429:
                # A quota response is an upstream failure, but retrying it here
                # only amplifies provider throttling. Durable jobs own retries.
                self._circuit_result(hostname, success=False)
            elif method in {"GET", "HEAD"} and response.status_code in {502, 503, 504}:
                self._circuit_result(hostname, success=False)
                if attempt + 1 < attempts and time.monotonic() < deadline:
                    time.sleep(min(0.15 * (2**attempt), max(0.0, deadline - time.monotonic())))
                    continue
            elif response.status_code >= 500:
                self._circuit_result(hostname, success=False)
            else:
                self._circuit_result(hostname, success=True)
            return response
        raise requests.ConnectionError("Public HTTPS request failed")

    def _request_once(self, method: str, url: str, *, params=None, data=None, json_body=None, timeout=30,
                allow_redirects=False, allow_redirect_status=False,
                max_bytes=16 * 1024 * 1024, headers=None) -> requests.Response:
        parsed, hostname = self._validated_url(url)
        if allow_redirects:
            raise ValueError("Only direct public HTTPS destinations are allowed")
        address = public_addresses(hostname)[0]
        query = parsed.query
        if params:
            query += ("&" if query else "") + urlencode(params, doseq=True)
        target = urlunsplit(("", "", parsed.path or "/", query, ""))
        if data is not None and json_body is not None:
            raise ValueError("Choose either form data or JSON body")
        body = urlencode(data, doseq=True).encode() if data is not None else None
        if json_body is not None:
            body = json.dumps(json_body, allow_nan=False, separators=(",", ":")).encode()
        supplied_headers = headers or {}
        if any(key.lower() not in {"user-agent", "accept", "content-type", "authorization", "range", "if-range", "x-amz-target", "ocp-apim-subscription-key"} for key in supplied_headers):
            raise ValueError("Public HTTPS accepts only application identification headers")
        headers = {**supplied_headers, "Host": hostname, "Accept-Encoding": "identity"}
        if body is not None and json_body is None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
        seconds = self._timeout_seconds(timeout)
        deadline = time.monotonic() + seconds
        pool = urllib3.HTTPSConnectionPool(
            address, port=443, server_hostname=hostname, assert_hostname=hostname,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), maxsize=1, block=True,
        )
        upstream = None
        try:
            upstream = pool.urlopen(method, target, body=body, headers=headers,
                timeout=urllib3.Timeout(connect=min(seconds, 5), read=min(seconds, 10), total=seconds),
                redirect=False, retries=False, preload_content=False)
            if 300 <= upstream.status < 400 and not allow_redirect_status:
                raise ValueError("Public HTTPS redirects are not allowed")
            content_encoding = str(upstream.headers.get("Content-Encoding", "identity")).lower()
            if content_encoding not in {"", "identity"}:
                raise ValueError("Encoded public HTTPS responses are not allowed")
            raw_response = getattr(upstream, "_fp", None)
            read_chunk = getattr(raw_response, "read1", None)
            if not callable(read_chunk):
                raise requests.RequestException("Streaming response does not support bounded reads")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise requests.Timeout("Public HTTPS deadline exceeded")
                # Read one socket buffer. read(amount) can wait for a full chunk
                # when a peer drips bytes just below read_timeout.
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Public HTTPS response exceeds the size limit")
                chunks.append(chunk)
            response = requests.Response()
            response.status_code = upstream.status
            response.headers.update(upstream.headers)
            response._content = b"".join(chunks)
            response.url = url
            response.encoding = requests.utils.get_encoding_from_headers(response.headers)
            return response
        except urllib3.exceptions.TimeoutError as exc:
            raise requests.Timeout("Public HTTPS request timed out") from exc
        except urllib3.exceptions.HTTPError as exc:
            raise requests.RequestException("Public HTTPS request failed") from exc
        finally:
            if upstream is not None:
                upstream.close()
            pool.close()

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs) -> requests.Response:
        return self.request("HEAD", url, max_bytes=0, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def open_stream(self, url: str, *, max_bytes: int,
                    allowed_redirect_hosts: set[str] | frozenset[str],
                    timeout=300, headers=None) -> BoundedStreamingResponse:
        """Open a bounded stream over pinned HTTPS sockets with allowlisted redirects."""
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("A positive stream size limit is required")
        _, initial_hostname = self._validated_url(url)
        redirect_domains = {
            str(domain).strip().encode("idna").decode("ascii").lower().lstrip(".")
            for domain in allowed_redirect_hosts
        }
        if not redirect_domains:
            raise ValueError("At least one redirect domain must be allowed")
        supplied_headers = headers or {}
        if any(key.lower() not in {"user-agent", "accept", "authorization"} for key in supplied_headers):
            raise ValueError("Stream header is not allowed")
        current_headers = {key.title(): value for key, value in supplied_headers.items()}
        current_headers["Accept-Encoding"] = "identity"
        seconds = self._timeout_seconds(timeout, maximum=600.0)
        deadline = time.monotonic() + seconds
        current_url = url
        self._circuit_before(initial_hostname)
        for redirect_count in range(4):
            parsed, hostname = self._validated_url(current_url)
            if redirect_count and not any(
                hostname == domain or hostname.endswith("." + domain) for domain in redirect_domains
            ):
                self._circuit_result(initial_hostname, success=False)
                raise ValueError("Stream redirect destination is not allowed")
            address = public_addresses(hostname)[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._circuit_result(initial_hostname, success=False)
                raise requests.Timeout("Public HTTPS stream deadline exceeded")
            pool = urllib3.HTTPSConnectionPool(
                address, port=443, server_hostname=hostname, assert_hostname=hostname,
                cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), maxsize=1, block=True,
            )
            upstream = None
            try:
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                upstream = pool.urlopen(
                    "GET", target, headers={**current_headers, "Host": hostname},
                    timeout=urllib3.Timeout(connect=min(remaining, 5), read=min(remaining, 30), total=remaining),
                    redirect=False, retries=False, preload_content=False,
                )
                if 300 <= upstream.status < 400:
                    location = upstream.headers.get("Location")
                    if not location or redirect_count >= 3:
                        raise ValueError("Public HTTPS stream redirect is invalid")
                    from urllib.parse import urljoin
                    next_url = urljoin(current_url, location)
                    _, next_host = self._validated_url(next_url)
                    if not any(next_host == domain or next_host.endswith("." + domain) for domain in redirect_domains):
                        raise ValueError("Stream redirect destination is not allowed")
                    if next_host != initial_hostname:
                        current_headers.pop("Authorization", None)
                    upstream.close()
                    pool.close()
                    current_url = next_url
                    continue
                content_encoding = str(upstream.headers.get("Content-Encoding", "identity")).lower()
                if content_encoding not in {"", "identity"}:
                    raise ValueError("Encoded public HTTPS streams are not allowed")
                content_length = upstream.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Invalid stream content length") from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise ValueError("Public HTTPS stream exceeds the size limit")
                return BoundedStreamingResponse(
                    upstream, pool, url=current_url, hostname=initial_hostname,
                    deadline=deadline, max_bytes=max_bytes,
                )
            except urllib3.exceptions.TimeoutError as exc:
                if upstream is not None:
                    upstream.close()
                pool.close()
                self._circuit_result(initial_hostname, success=False)
                raise requests.Timeout("Public HTTPS stream timed out") from exc
            except urllib3.exceptions.HTTPError as exc:
                if upstream is not None:
                    upstream.close()
                pool.close()
                self._circuit_result(initial_hostname, success=False)
                raise requests.RequestException("Public HTTPS stream failed") from exc
            except Exception:
                if upstream is not None:
                    upstream.close()
                pool.close()
                self._circuit_result(initial_hostname, success=False)
                raise
        self._circuit_result(initial_hostname, success=False)
        raise requests.RequestException("Too many public HTTPS stream redirects")

    def download_to_file(self, url: str, destination, *, max_bytes: int,
                         allowed_redirect_hosts: set[str] | frozenset[str],
                         timeout=300, headers=None, same_host_redirects: bool = False) -> requests.Response:
        """Download a bounded file with the shared one-retry GET policy and circuit breaker."""
        _, hostname = self._validated_url(url)
        seconds = self._timeout_seconds(timeout, maximum=600.0)
        deadline = time.monotonic() + seconds
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("Public HTTPS download deadline exceeded")
            self._circuit_before(hostname)
            try:
                response = self._download_to_file_once(
                    url,
                    destination,
                    max_bytes=max_bytes,
                    allowed_redirect_hosts=allowed_redirect_hosts,
                    timeout=remaining,
                    headers=headers,
                    same_host_redirects=same_host_redirects,
                )
            except (requests.RequestException, OSError):
                self._circuit_result(hostname, success=False)
                if attempt or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.15, deadline - time.monotonic()))
                continue
            except Exception:
                self._circuit_result(hostname, success=False)
                raise

            retryable_status = response.status_code in {502, 503, 504}
            self._circuit_result(hostname, success=not retryable_status and response.status_code != 429)
            if retryable_status and not attempt and time.monotonic() < deadline:
                time.sleep(min(0.15, deadline - time.monotonic()))
                continue
            return response
        raise requests.ConnectionError("Public HTTPS download failed")

    def _download_to_file_once(self, url: str, destination, *, max_bytes: int,
                         allowed_redirect_hosts: set[str] | frozenset[str],
                         timeout=300, headers=None, same_host_redirects: bool = False) -> requests.Response:
        """Stream one bounded public HTTPS download to a caller-owned temporary file.

        Redirects are limited to explicit provider domains. Each hop is resolved
        and pinned independently. Authorization may be sent only to the original
        host and is stripped before any cross-host redirect. The method does not
        use system proxies/netrc.
        """
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("A positive download size limit is required")
        parsed, hostname = self._validated_url(url)
        redirect_domains = {
            str(domain).strip().encode("idna").decode("ascii").lower().lstrip(".")
            for domain in allowed_redirect_hosts
        }
        if not redirect_domains:
            raise ValueError("At least one redirect domain must be allowed")
        supplied_headers = headers or {}
        if any(key.lower() not in {"user-agent", "accept", "authorization"} for key in supplied_headers):
            raise ValueError("Download header is not allowed")
        normalized_headers = {key.title(): value for key, value in supplied_headers.items()}
        safe_headers = {**normalized_headers, "Accept-Encoding": "identity"}
        seconds = self._timeout_seconds(timeout, maximum=600.0)
        deadline = time.monotonic() + seconds
        current_url = url
        initial_hostname = hostname
        current_headers = dict(safe_headers)
        target_path = os.fspath(destination)
        redirect_count = 0
        created_file = False
        try:
            while True:
                parsed, hostname = self._validated_url(current_url)
                if redirect_count and not any(
                    hostname == domain or hostname.endswith("." + domain)
                    for domain in redirect_domains
                ):
                    raise ValueError("Download redirect destination is not allowed")
                address = public_addresses(hostname)[0]
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                pool = urllib3.HTTPSConnectionPool(
                    address, port=443, server_hostname=hostname, assert_hostname=hostname,
                    cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), maxsize=1, block=True,
                )
                upstream = None
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise requests.Timeout("Public HTTPS download deadline exceeded")
                    upstream = pool.urlopen(
                        "GET", target, headers={**current_headers, "Host": hostname},
                        timeout=urllib3.Timeout(connect=min(remaining, 5), read=min(remaining, 30), total=remaining),
                        redirect=False, retries=False, preload_content=False,
                    )
                    if 300 <= upstream.status < 400:
                        location = upstream.headers.get("Location")
                        if not location or redirect_count >= 3:
                            raise ValueError("Public HTTPS download has an invalid redirect")
                        from urllib.parse import urljoin
                        next_url = urljoin(current_url, location)
                        next_parsed, next_host = self._validated_url(next_url)
                        if same_host_redirects and next_host != initial_hostname:
                            raise ValueError("Public HTTPS download redirect must stay on the original host")
                        if not any(next_host == domain or next_host.endswith("." + domain) for domain in redirect_domains):
                            raise ValueError("Download redirect destination is not allowed")
                        if next_host != initial_hostname:
                            current_headers.pop("Authorization", None)
                        current_url = next_url
                        redirect_count += 1
                        continue
                    response = requests.Response()
                    response.status_code = upstream.status
                    response.headers.update(upstream.headers)
                    response.url = current_url
                    if not 200 <= upstream.status < 300:
                        response._content = b""
                        return response
                    content_encoding = str(upstream.headers.get("Content-Encoding", "identity")).lower()
                    if content_encoding not in {"", "identity"}:
                        raise ValueError("Encoded public HTTPS downloads are not allowed")
                    content_length = upstream.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            declared_size = int(content_length)
                        except (TypeError, ValueError) as exc:
                            raise ValueError("Invalid download content length") from exc
                        if declared_size < 0 or declared_size > max_bytes:
                            raise ValueError("Public HTTPS download exceeds the size limit")
                    raw_response = getattr(upstream, "_fp", None)
                    read_chunk = getattr(raw_response, "read1", None)
                    if not callable(read_chunk):
                        raise requests.RequestException("Streaming response does not support bounded reads")
                    byte_count = 0
                    with open(target_path, "xb") as output:
                        created_file = True
                        while True:
                            if time.monotonic() >= deadline:
                                raise requests.Timeout("Public HTTPS download deadline exceeded")
                            chunk = read_chunk(64 * 1024)
                            if not chunk:
                                break
                            byte_count += len(chunk)
                            if byte_count > max_bytes:
                                raise ValueError("Public HTTPS download exceeds the size limit")
                            output.write(chunk)
                    if content_length is not None and byte_count != int(content_length):
                        raise requests.RequestException("Incomplete public HTTPS download")
                    return response
                finally:
                    if upstream is not None:
                        upstream.close()
                    pool.close()
        except urllib3.exceptions.TimeoutError as exc:
            if created_file:
                try:
                    os.unlink(target_path)
                except FileNotFoundError:
                    pass
            raise requests.Timeout("Public HTTPS download timed out") from exc
        except urllib3.exceptions.HTTPError as exc:
            if created_file:
                try:
                    os.unlink(target_path)
                except FileNotFoundError:
                    pass
            raise requests.RequestException("Public HTTPS download failed") from exc
        except Exception:
            if created_file:
                try:
                    os.unlink(target_path)
                except FileNotFoundError:
                    pass
            raise


public_http = PublicHTTPS()
