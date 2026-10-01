from unittest.mock import MagicMock, patch

from app.services import imagery_service


def test_gee_scene_list_aggregates_count_and_fields_in_one_request():
    collection = MagicMock()
    collection.filterBounds.return_value = collection
    collection.filterDate.return_value = collection
    collection.sort.return_value = collection
    collection.limit.return_value = collection
    collection.aggregate_array.side_effect = lambda name: MagicMock(name=f"aggregate-{name}")
    aoi = MagicMock(name="aoi")

    with patch.object(imagery_service, "ee") as ee, patch.object(
        imagery_service, "create_geometry_from_payload", return_value=aoi
    ):
        ee.ImageCollection.return_value = collection
        ee.Dictionary.return_value.getInfo.return_value = {
            "count": 1,
            "ids": ["S2_TEST"],
            "times": [1_735_689_600_000],
            "clouds": [12.3],
        }
        result = imagery_service.list_scenes(
            {
                "satellite": "sentinel2",
                "aoi": {"geojson": {}},
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "scene_limit": 25,
            }
        )

    ee.Dictionary.return_value.getInfo.assert_called_once_with()
    collection.limit.assert_called_once_with(25)
    assert result["count"] == 1
    assert result["truncated"] is False
    assert result["scenes"][0]["id"] == "S2_TEST"
    assert result["scenes"][0]["cloud_cover_pct"] == 12.3
