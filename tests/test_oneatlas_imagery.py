from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.services import oneatlas_imagery_service as oneatlas
from app.services.gee_common import AnalysisError


@pytest.fixture(autouse=True)
def reset_oneatlas_caches(monkeypatch):
    oneatlas._TOKEN = None
    with oneatlas._SCENE_LOCK:
        oneatlas._SCENES.clear()
    monkeypatch.setattr(
        oneatlas,
        "get_settings",
        lambda: Settings(oneatlas_api_key="test-api-key", oneatlas_workspace_id="workspace-123"),
    )
    yield
    oneatlas._TOKEN = None
    with oneatlas._SCENE_LOCK:
        oneatlas._SCENES.clear()


def _client_factory(handler, seen):
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _feature(*, rights=True):
    return {
        "type": "Feature",
        "id": "private-provider-scene-42",
        "bbox": [100.0, -2.0, 101.0, -1.0],
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[100.0, -2.0], [101.0, -2.0], [101.0, -1.0], [100.0, -1.0], [100.0, -2.0]]],
        },
        "properties": {
            "id": "private-provider-scene-42",
            "acquisitionDate": "2025-01-02T03:04:05+02:00",
            "resolution": 0.3,
            "cloudCover": "12.25",
            "platform": "Pleiades Neo",
            "title": "Scene 42",
        },
        "_links": {
            "wmts": {
                "href": "https://access.foundation.api.oneatlas.airbus.com/api/v1/items/123e4567-e89b-12d3-a456-426614174000/wmts"
            }
        },
        "rights": {"wmts": rights},
    }


def test_official_search_returns_opaque_scene_and_internal_authenticated_preview():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "authenticate.foundation.api.oneatlas.airbus.com":
            return httpx.Response(200, json={"access_token": "never-return-this-token", "expires_in": 3600})
        assert request.url.host == "search.foundation.api.oneatlas.airbus.com"
        assert request.headers["Authorization"] == "Bearer never-return-this-token"
        assert request.url.params["workspaceId"] == "workspace-123"
        body = __import__("json").loads(request.content)
        assert body["processingLevel"] == "SENSOR"
        assert body["relation"] == "intersects"
        assert body["cloudCover"] == "[0,20]"
        return httpx.Response(200, json={"features": [_feature()], "itemsPerPage": 500})

    result = oneatlas.list_scenes(
        {
            "aoi": {"west": 100, "south": -2, "east": 101, "north": -1},
            "start_date": "2025-01-01",
            "end_date": "2025-01-03",
            "max_cloud_cover": 20,
        },
        SimpleNamespace(base_url="https://savegeo.example/api/"),
        client_factory=_client_factory(handler, calls),
    )

    assert len(calls) == 2
    assert calls[0].url == oneatlas._AUTH_URL
    assert calls[0].read().decode() == "apikey=test-api-key&grant_type=api_key&client_id=IDP"
    scene = result["scenes"][0]
    assert scene["id"].startswith("oneatlas:")
    assert scene["acquired_at"] == "2025-01-02T01:04:05.000Z"
    assert scene["resolution_m"] == 0.3
    assert scene["cloud_cover_pct"] == 12.2
    assert scene["assets"][0]["href"].startswith(
        "https://savegeo.example/api/api/imagery/oneatlas-tiles/oneatlas:"
    )
    assert "private-provider-scene-42" not in repr(scene)
    assert "oneatlas.airbus.com" not in repr(scene)
    assert "never-return-this-token" not in repr(result)
    assert result["satellite"]["capabilities"]["preview_kind"] == "provider_tiles"


def test_scene_without_wmts_right_remains_metadata_only():
    def handler(request):
        if request.url.host == "authenticate.foundation.api.oneatlas.airbus.com":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json={"features": [_feature(rights=False)]})

    result = oneatlas.list_scenes(
        {"aoi": {"west": 100, "south": -2, "east": 101, "north": -1}, "start_date": "2025-01-01", "end_date": "2025-01-03"},
        SimpleNamespace(base_url="https://savegeo.example/"),
        client_factory=_client_factory(handler, []),
    )
    scene = result["scenes"][0]
    assert scene["assets"] == []
    assert scene["default_asset_key"] is None
    with pytest.raises(AnalysisError, match="tidak memberi hak WMTS"):
        oneatlas.get_scene_tile({"scene_id": scene["id"]}, SimpleNamespace(base_url="https://savegeo.example/"))


def test_wmts_preview_uses_cached_scene_and_rejects_bad_tile_coordinates():
    scene_ref = oneatlas._scene_ref("private-provider-scene-42")
    oneatlas._remember_scene(
        scene_ref,
        "private-provider-scene-42",
        "https://access.foundation.api.oneatlas.airbus.com/api/v1/items/123e4567-e89b-12d3-a456-426614174000/wmts",
        0.3,
    )
    png = b"\x89PNG\r\n\x1a\nmock"
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "authenticate.foundation.api.oneatlas.airbus.com":
            return httpx.Response(200, json={"access_token": "mock-preview-token", "expires_in": 3600})
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=png)

    tile = oneatlas.render_tile(scene_ref, 4, 6, 7, client_factory=_client_factory(handler, seen))
    assert tile == png
    assert len(seen) == 2
    assert seen[1].url.path.endswith("/rgb/EPSG3857/4/6/7.png")
    assert seen[1].headers["Authorization"] == "Bearer mock-preview-token"
    with pytest.raises(AnalysisError, match="coordinates"):
        oneatlas.render_tile(scene_ref, 4, 16, 0, client_factory=_client_factory(handler, []))


@pytest.mark.parametrize(
    "href",
    [
        "http://access.foundation.api.oneatlas.airbus.com/api/v1/items/123e4567-e89b-12d3-a456-426614174000/wmts",
        "https://evil.example/api/v1/items/123e4567-e89b-12d3-a456-426614174000/wmts",
        "https://access.foundation.api.oneatlas.airbus.com/api/v1/items/not-a-uuid/wmts",
        "https://access.foundation.api.oneatlas.airbus.com/api/v1/items/123e4567-e89b-12d3-a456-426614174000/wmts?token=secret",
    ],
)
def test_wmts_link_only_accepts_documented_https_access_origin(href):
    feature = _feature()
    feature["_links"]["wmts"]["href"] = href
    assert oneatlas._wmts_link(feature) is None


def test_missing_configuration_fails_before_network(monkeypatch):
    monkeypatch.setattr(oneatlas, "get_settings", lambda: Settings())
    with pytest.raises(AnalysisError, match="ONEATLAS_API_KEY"):
        oneatlas.list_scenes({"aoi": {}, "start_date": "2025-01-01", "end_date": "2025-01-02"})
