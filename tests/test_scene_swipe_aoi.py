"""Scene comparison must create both GEE tiles from the same exact AOI."""
from unittest.mock import MagicMock, patch, sentinel

from app.services import imagery_service as service


def test_both_scene_tiles_are_clipped_before_url_creation():
    payload = {"geojson": {"type": "Polygon", "coordinates": [
        [[110, -2], [111, -2], [111, -1], [110, -2]],
    ]}}
    metadata = {"name": "Test", "gee_collection": "test-scenes",
                "visualization": "single_band", "band": "B1",
                "vis_min": 0, "vis_max": 1, "palette": ["black", "white"]}
    scenes = [MagicMock(name="before"), MagicMock(name="after")]
    with patch.object(service, "ee") as ee, \
         patch.object(service, "resolve_imagery_provider", return_value="test"), \
         patch.object(service, "get_imagery_provider_meta", return_value=metadata), \
         patch.object(service, "create_geometry_from_payload", return_value=sentinel.aoi) as geometry, \
         patch.object(service, "_apply_super_resolution", side_effect=lambda img, *_: (img, {})), \
         patch.object(service, "get_tile_url", side_effect=[{"tile_url": "before-url"}, {"tile_url": "after-url"}]) as tiles:
        matches = ee.ImageCollection.return_value.filter.return_value
        matches.size.return_value.getInfo.return_value = 1
        matches.first.side_effect = scenes
        results = [service.get_scene_tile({"satellite": "test", "scene_id": scene, "aoi": payload})
                   for scene in ("before-id", "after-id")]
        assert geometry.call_count == 2
        for call in geometry.call_args_list:
            assert call.args == (payload,)
        for index, scene in enumerate(scenes):
            scene.clip.assert_called_once_with(sentinel.aoi)
            assert tiles.call_args_list[index].args[0] is scene.clip.return_value
        assert [result["tile_url"] for result in results] == ["before-url", "after-url"]
