from app.services.boundary_provider import CachedBoundaryProvider, normalize_geometry


def _fixture():
    # SP3STAB-like provider order: [latitude, longitude].
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"code": "62"},
            "geometry": {"type": "Polygon", "coordinates": [[[-1.0, 113.0], [-1.0, 114.0], [-2.0, 114.0], [-2.0, 113.0], [-1.0, 113.0]]]},
        }],
    }


def test_sp3stab_geometry_is_normalized_as_a_whole_geometry():
    result = normalize_geometry(_fixture())
    assert result.validation_status == "valid"
    assert result.transformation == "lat-lng to lng-lat"
    assert result.geojson["features"][0]["geometry"]["coordinates"][0][0] == [113.0, -1.0]
    assert len(result.checksum) == 64


def test_geojson_axis_can_be_explicitly_kept_for_validated_provider():
    fixture = _fixture()
    fixture["features"][0]["geometry"]["coordinates"] = [[[113.0, -1.0], [114.0, -1.0], [114.0, -2.0], [113.0, -2.0], [113.0, -1.0]]]
    result = normalize_geometry(fixture, provider_axis="lng-lat")
    assert result.transformation == "none"
    assert result.geojson["features"][0]["geometry"]["coordinates"][0][0] == [113.0, -1.0]


def test_boundary_cache_returns_stale_geometry_when_provider_fails():
    class Provider:
        def __init__(self):
            self.fail = False

        def geometry(self, level, code, parent_code=None):
            if self.fail:
                raise TimeoutError("provider timeout")
            return {"type": "Polygon", "coordinates": [[[113.0, -1.0], [114.0, -1.0], [114.0, -2.0], [113.0, -2.0], [113.0, -1.0]]]}

    provider = Provider()
    cache = CachedBoundaryProvider(provider, ttl_seconds=-1)
    geometry, stale = cache.geometry("province", "62")
    assert stale is False
    provider.fail = True
    fallback, stale = cache.geometry("province", "62")
    assert stale is True
    assert fallback == geometry
