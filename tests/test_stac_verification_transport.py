from types import SimpleNamespace

import pytest

from scripts import verify_imagery_stac_samgeo as verification


def test_live_stac_asset_probe_uses_a_bounded_range_request(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(content=b"II\x2a\x00", raise_for_status=lambda: None)

    monkeypatch.setattr(verification.public_http, "get", get)

    assert verification.verify_tiff_asset_signature("https://assets.example/scene.tif?sig=test", "test") == 4
    assert calls == [(
        "https://assets.example/scene.tif?sig=test",
        {"headers": {"Range": "bytes=0-15"}, "timeout": 60, "max_bytes": 16},
    )]


def test_live_stac_asset_probe_rejects_non_tiff(monkeypatch):
    monkeypatch.setattr(
        verification.public_http,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(content=b"<html>", raise_for_status=lambda: None),
    )

    with pytest.raises(AssertionError, match="source is not TIFF"):
        verification.verify_tiff_asset_signature("https://assets.example/scene.tif", "test")
