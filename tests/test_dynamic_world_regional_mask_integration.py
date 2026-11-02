"""Exercise regional masks on real Earth Engine rasters when explicitly enabled."""
import os

import ee
import pytest

from app.db.session import SessionLocal
from app.services import gee_service
from app.services.dynamic_world_rules import mask_indonesia_snow_ice

pytestmark = pytest.mark.skipif(
    os.environ.get("SAVEGEO_RUN_GEE_INTEGRATION") != "1",
    reason="Set SAVEGEO_RUN_GEE_INTEGRATION=1 with an Earth Engine credential to run",
)


def test_regional_rule_preserves_land_and_cloud_masks_while_filtering_tropical_snow():
    with SessionLocal() as db:
        assert gee_service.initialize_ee(db)
    cases = [
        ("flores_land", [120.46, -8.61], 6, False, True),
        ("flores_snow", [120.46, -8.61], 8, False, False),
        ("puncak_jaya_snow", [137.159533, -4.078883], 8, False, True),
        ("outside_indonesia_snow", [8.0, 47.0], 8, False, True),
        ("cloud_mask_stays_invalid", [120.46, -8.61], 6, True, False),
    ]
    results = []
    for name, coordinates, class_id, masked, _ in cases:
        aoi = ee.Geometry.Point(coordinates).buffer(1000)
        image = ee.Image.constant(class_id).rename("label").clip(aoi)
        if masked:
            image = image.updateMask(ee.Image.constant(0))
        filtered = mask_indonesia_snow_ice(image)
        count = filtered.reduceRegion(ee.Reducer.count(), aoi, 100, maxPixels=10000).get("label")
        results.append(ee.Feature(None, {"name": name, "count": count}))
    counts = {row["properties"]["name"]: row["properties"].get("count", 0)
              for row in ee.FeatureCollection(results).getInfo()["features"]}
    for name, _, _, _, expected in cases:
        assert bool(counts[name]) is expected, (name, counts[name])
