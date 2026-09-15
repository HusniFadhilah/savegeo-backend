"""Small, opt-in live GEE resolution check using configured credentials.

Run from backend. Downloads XYZ tiles, never thumbnails. Credentials and
signed tile URLs are not written to the report or printed.
"""
import io
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ee
import numpy as np
import requests
from PIL import Image
from app.core.config import get_settings
from app.services.gee_common import build_s2_cloud_masked_collection
from app.services.imagery_resolution import native_median
from app.services.imagery_service import get_scene_tile


def main():
    settings = get_settings()
    ee.Initialize(ee.ServiceAccountCredentials(settings.gee_service_account, settings.gee_key_file), project=settings.gee_project_id)
    feature = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
        [[115.19, -8.68], [115.25, -8.68], [115.25, -8.62], [115.19, -8.62], [115.19, -8.68]]
    ]}}
    aoi = ee.Geometry(feature["geometry"])
    output = Path(__file__).resolve().parents[2] / "frontend/test-results/live-resolution"
    output.mkdir(parents=True, exist_ok=True)
    z = 14
    x = int((115.22 + 180) / 360 * 2**z)
    y = int((1 - math.asinh(math.tan(math.radians(-8.65))) / math.pi) / 2 * 2**z)
    results = []
    for side, year in (("before", 2024), ("after", 2025)):
        start, end = f"{year}-06-01", f"{year}-09-01"
        collection = build_s2_cloud_masked_collection(aoi, start, end, 60)
        count = collection.size().getInfo()
        assert count > 0
        scene = collection.sort("CLOUDY_PIXEL_PERCENTAGE").first()
        scene_id = scene.get("system:index").getInfo()
        composite = native_median(collection, ["B4", "B3", "B2", "B8", "B11"]).clip(aoi)
        scales = ee.Dictionary({band: composite.select(band).projection().nominalScale() for band in ("B4", "B8", "B11")}).getInfo()
        assert abs(scales["B4"]-10) < .01 and abs(scales["B8"]-10) < .01 and abs(scales["B11"]-20) < .01
        tile = get_scene_tile({"satellite": "sentinel2", "scene_id": scene_id, "aoi": {"geojson": feature}, "cloud_mask_technique": "scl", "super_resolution": "off"})
        detail = None
        for dx in range(-2, 3):
            for dy in range(-1, 2):
                tx, ty = x+dx, y+dy
                response = requests.get(tile["tile_url"].format(z=z, x=tx, y=ty), timeout=45)
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content)).convert("RGBA")
                assert image.size == (256, 256)
                destination = output / side / str(z) / str(tx)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / f"{ty}.png").write_bytes(response.content)
                if dx == dy == 0:
                    pixels = np.asarray(image)
                    valid = pixels[:, :, 3] > 0
                    assert valid.any()
                    detail = float(pixels[:, :, :3][valid].std())
                    assert detail > 2, "Expected spatially varying native-resolution imagery"
        results.append({"side": side, "scene_id": scene_id, "dataset": tile["dataset"], "scene_count": count, "period": [start, end], "native_scales": scales, "tile_zoom": z, "center_tile_std": detail})
    (output / "results.json").write_text(json.dumps({"aoi": feature, "results": results}, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
