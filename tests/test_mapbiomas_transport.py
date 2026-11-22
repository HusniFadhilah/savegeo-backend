from __future__ import annotations

from types import SimpleNamespace

from app.registries import landcover_dataset_registry as registry


def test_mapbiomas_probe_uses_bounded_pinned_transport(monkeypatch):
    calls = []

    def head(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(registry.public_http, "head", head)

    uri, metadata = registry.mapbiomas_indonesia_geotiff_uri(2023)

    assert uri.startswith("gs://mapbiomas-public/")
    assert metadata["collection"] == "Collection 3"
    assert len(calls) == 1
    assert calls[0][0].startswith("https://storage.googleapis.com/mapbiomas-public/")
    assert calls[0][1] == {
        "timeout": 8,
        "allow_redirects": False,
        "max_bytes": 1024,
    }
