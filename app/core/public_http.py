"""Bounded HTTPS transport for public, untrusted destinations.

Resolve once, reject every non-public answer and connect to that exact address.
TLS still verifies the original hostname and sends it as SNI. This transport
does not inherit proxy credentials, netrc or redirect behavior from the host.
Operator-configured internal services (e.g. Ollama) do not use this client.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import certifi
import requests
import urllib3


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


class PublicHTTPS:
    def request(self, method: str, url: str, *, params=None, data=None, timeout=30,
                allow_redirects=False, max_bytes=16 * 1024 * 1024, headers=None) -> requests.Response:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443}
                or any(ord(c) < 33 for c in url) or "\\" in url or allow_redirects):
            raise ValueError("Only direct public HTTPS destinations are allowed")
        hostname = parsed.hostname.encode("idna").decode("ascii")
        address = public_addresses(hostname)[0]
        query = parsed.query
        if params:
            query += ("&" if query else "") + urlencode(params, doseq=True)
        target = urlunsplit(("", "", parsed.path or "/", query, ""))
        body = urlencode(data, doseq=True).encode() if data is not None else None
        supplied_headers = headers or {}
        if any(key.lower() not in {"user-agent", "accept"} for key in supplied_headers):
            raise ValueError("Public HTTPS accepts only application identification headers")
        headers = {**supplied_headers, "Host": hostname, "Accept-Encoding": "identity"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        seconds = min(max(float(timeout), 0.1), 60.0)
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
            if 300 <= upstream.status < 400:
                raise ValueError("Public HTTPS redirects are not allowed")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise requests.Timeout("Public HTTPS deadline exceeded")
                # read1 returns after one socket read. read(amount) can wait forever
                # for a full chunk when a peer drips bytes just below read_timeout.
                chunk = upstream.read1(64 * 1024, decode_content=True)
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
        except urllib3.exceptions.HTTPError as exc:
            raise requests.RequestException("Public HTTPS request failed") from exc
        finally:
            if upstream is not None:
                upstream.close()
            pool.close()

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)


public_http = PublicHTTPS()
