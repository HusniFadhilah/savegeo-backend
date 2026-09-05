"""Live STAC search, source download, PNG pixel checks, optional real SAM inference.

Run from backend: python scripts/verify_imagery_stac_samgeo.py [--samgeo]
"""
import io
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import numpy as np
from PIL import Image

from app.services import imagery_service as imagery
from app.services import samgeo_service


def aoi(west, south, east, north):
    return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
        [[west, south], [east, south], [east, north], [west, north], [west, south]]
    ]}}


def main():
    imagery._MAX_SCENES = 2
    fixtures = [
        ("earth-search", "https://earth-search.aws.element84.com/v1", "sentinel-2-l2a", aoi(107.6, -6.91, 107.61, -6.9), "2024-07-01", "2024-07-15"),
        ("planetary-sentinel", "https://planetarycomputer.microsoft.com/api/stac/v1", "sentinel-2-l2a", aoi(107.6, -6.91, 107.61, -6.9), "2024-07-01", "2024-07-15"),
        ("planetary-naip", "https://planetarycomputer.microsoft.com/api/stac/v1", "naip", aoi(-122.46, 37.768, -122.458, 37.770), "2020-01-01", "2024-12-31"),
    ]
    results = []
    for label, url, collection, area, start, end in fixtures:
        response = imagery.list_scenes({"satellite": "stac_catalog", "stac_catalog_url": url,
            "stac_collections": collection, "aoi": area, "start_date": start, "end_date": end})
        assert response["count"] > 0, label
        scene = response["scenes"][0]
        box = imagery._aoi_payload_to_bbox(area)
        zoom = 17 if collection == "naip" else 14
        longitude, latitude = (box[0]+box[2])/2, (box[1]+box[3])/2
        x = int((longitude + 180) / 360 * 2**zoom)
        y = int((1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * 2**zoom)
        asset = scene["default_asset_key"]
        png = imagery.render_stac_cog_tile(scene["id"], zoom, x, y, asset)
        assert png, label
        pixels = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
        assert (pixels[:, :, 3] > 0).any(), f"{label}: transparent tile"
        assert float(pixels[:, :, :3].std()) > 1, f"{label}: blank tile"
        href = imagery.get_stac_asset_download_url(scene["id"], asset)
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            with client.stream("GET", href, headers={"Range": "bytes=0-15"}) as source:
                source.raise_for_status()
                signature = next(source.iter_bytes(chunk_size=16))
                assert signature[:2] in (b"II", b"MM"), f"{label}: source is not TIFF"
        entry = {"catalog": label, "scene": scene["id"], "asset": asset, "scenes": response["count"],
                 "png_bytes": len(png), "pixel_std": round(float(pixels[:, :, :3].std()), 2)}
        print(json.dumps(entry), flush=True)
        results.append(entry)
        if collection == "naip" and "--samgeo" in sys.argv:
            job = samgeo_service.start_job({"item_url": scene["id"], "asset_key": asset, "aoi": area})
            print("SAM job: " + job["job_id"], flush=True)
            deadline = time.monotonic() + 1000
            while time.monotonic() < deadline:
                state = samgeo_service.get_job(job["job_id"])
                if state["status"] != "running":
                    assert state["status"] == "complete", state
                    assert state["result"]["features"], "SAM returned no polygons"
                    results.append({"samgeo": state["result"]["metadata"], "polygons": len(state["result"]["features"])})
                    print(json.dumps(results[-1]), flush=True)
                    break
                time.sleep(2)
            else:
                raise TimeoutError("SAM did not finish")
    target = Path(__file__).resolve().parents[1] / "var" / "imagery-verification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
