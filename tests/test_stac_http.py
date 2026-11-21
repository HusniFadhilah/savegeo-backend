from __future__ import annotations

import pytest

from app.core import stac_http


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    @staticmethod
    def json():
        return {"type": "FeatureCollection", "features": []}


def test_stac_io_rejects_unlisted_hosts_before_network(monkeypatch):
    called = []
    monkeypatch.setattr(stac_http.public_http, "get", lambda *args, **kwargs: called.append(True))
    io = stac_http.BoundedStacApiIO(allowed_hosts={"planetarycomputer.microsoft.com"})

    with pytest.raises(ValueError, match="not allowed"):
        io.request("https://attacker.invalid/search")
    assert called == []


def test_stac_io_uses_bounded_transport_and_strips_unneeded_headers(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(stac_http.public_http, "validate_public_destination", lambda _url: None)
    monkeypatch.setattr(stac_http.public_http, "get", get)
    io = stac_http.BoundedStacApiIO(allowed_hosts={"catalog.example"}, timeout=12, max_bytes=4096)

    result = io.request("https://catalog.example/search", headers={"Authorization": "secret", "Accept": "application/geo+json"})

    assert '"features":[]' in result
    assert calls[0][1]["timeout"] == 12
    assert calls[0][1]["max_bytes"] == 4096
    assert calls[0][1]["headers"] == {"Accept": "application/geo+json"}


def test_stac_pagination_stops_at_configured_page_ceiling(monkeypatch):
    io = stac_http.BoundedStacApiIO(allowed_hosts={"catalog.example"}, max_pages=2)
    pages = iter([
        {"features": [{"id": "one"}], "links": [{"rel": "next", "href": "https://catalog.example/page/2"}]},
        {"features": [{"id": "two"}], "links": [{"rel": "next", "href": "https://catalog.example/page/3"}]},
        {"features": [{"id": "three"}], "links": []},
    ])
    calls = []

    def read_json(href, *args, **kwargs):
        calls.append(str(href))
        return next(pages)

    monkeypatch.setattr(io, "read_json", read_json)
    returned = list(io.get_pages("https://catalog.example/search"))
    assert len(returned) == 2
    assert len(calls) == 2


def test_planetary_computer_signing_uses_cached_token_without_network(monkeypatch):
    from planetary_computer.sas import SASToken, TOKEN_CACHE
    from planetary_computer.settings import Settings

    href = "https://storage.blob.core.windows.net/container/scene.tif"
    endpoint = "https://planetarycomputer.microsoft.com/api/sas/v1/token/storage/container"
    token = SASToken(**{"token": "sp=rl&se=2099-01-01T00:00:00Z&sv=2023-11-03&sr=c&sig=masked", "msft:expiry": "2099-01-01T00:00:00Z"})
    TOKEN_CACHE[endpoint] = token
    monkeypatch.setattr(Settings, "get", staticmethod(lambda: Settings(subscription_key=None, sas_url="https://planetarycomputer.microsoft.com/api/sas/v1/token")))
    monkeypatch.setattr(stac_http.public_http, "get", lambda *args, **kwargs: pytest.fail("cached SAS token should be reused"))

    signed = stac_http.sign_planetary_computer_href(href)

    assert signed.startswith(href + "?")
    assert "sig=masked" in signed
    TOKEN_CACHE.pop(endpoint, None)
