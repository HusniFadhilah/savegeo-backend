from app.providers.external_carbon_provider import _geojson_to_bbox


def test_geojson_to_bbox_accepts_feature():
    feature = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[106.1, -6.3], [106.4, -6.3], [106.4, -6.0], [106.1, -6.3]]],
        },
    }

    assert _geojson_to_bbox(feature) == (106.1, -6.3, 106.4, -6.0)


def test_geojson_to_bbox_accepts_feature_collection():
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [106.1, -6.3]},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [106.4, -6.0]},
            },
        ],
    }

    assert _geojson_to_bbox(collection) == (106.1, -6.3, 106.4, -6.0)
