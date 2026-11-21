from __future__ import annotations

from types import SimpleNamespace

from app.services import copernicus_geosave_service as service


class _Search:
    def __init__(self, item):
        self.item = item

    def items(self):
        return iter([self.item])


class _Client:
    def __init__(self, item):
        self.item = item
        self.search_args = None

    def search(self, **kwargs):
        self.search_args = kwargs
        return _Search(self.item)


def test_cdse_scene_search_uses_bounded_stac_client(monkeypatch):
    item = SimpleNamespace(
        id="scene-1",
        properties={"datetime": "2026-01-02T03:04:05Z", "eo:cloud_cover": 12.5},
    )
    client = _Client(item)
    monkeypatch.setattr(service, "_client", lambda: client)

    result = service.list_scenes({
        "satellite": "copernicus_s2_l2a",
        "aoi": {"west": 110, "south": -8, "east": 111, "north": -7},
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "max_cloud_cover": 20,
        "limit": 50,
    })

    assert client.search_args["collections"] == ["sentinel-2-l2a"]
    assert client.search_args["max_items"] == 50
    assert client.search_args["query"] == {"eo:cloud_cover": {"lte": 20.0}}
    assert result["scenes"] == [{"id": "scene-1", "acquired_at": "2026-01-02T03:04:05Z", "cloud_cover_pct": 12.5}]
    assert result["truncated"] is False
