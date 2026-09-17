from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.geospatial_outputs import GeoJSONFeatureCollection, TileJSONDocument


def test_geojson_contract_rejects_legacy_crs_and_accepts_wgs84_feature():
    value = GeoJSONFeatureCollection(
        features=[
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [110.1, -7.2]},
                "properties": {"observed_at": "2026-09-17T08:15:30.125Z"},
            }
        ]
    )
    assert value.type == "FeatureCollection"
    with pytest.raises(ValidationError):
        GeoJSONFeatureCollection(
            features=[{"type": "Feature", "geometry": None, "properties": {}, "crs": {}}]
        )


def test_tilejson_contract_validates_bounds_center_and_timestamp():
    value = TileJSONDocument(
        name="Carbon",
        version="1.0.0",
        tiles=["https://example.test/tiles/{z}/{x}/{y}.png"],
        bounds=[95, -11, 141, 6],
        center=[117, -2, 5],
        attribution="SaveGeo",
        created_at=datetime(2026, 9, 17, 8, 15, 30, 125000, tzinfo=UTC),
    )
    assert value.model_dump()["tilejson"] == "3.0.0"
    assert value.model_dump()["created_at"] == "2026-09-17T08:15:30.125Z"
