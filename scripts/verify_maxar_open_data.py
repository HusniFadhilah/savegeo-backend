"""Verify real Maxar scenes and save rendered COG tiles locally."""
import argparse
import io
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from app.services import imagery_service as imagery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["cianjur", "semeru", "maui", "jakarta"], default="cianjur")
    args = parser.parse_args()
    fixtures = {
        "cianjur": (107.09, -6.83, 107.10, -6.82, "2022-11-21", "2022-11-30"),
        "semeru": (113.01, -8.15, 113.02, -8.14, "2021-12-04", "2021-12-15"),
        "maui": (-156.68, 20.87, -156.67, 20.88, "2023-08-08", "2023-08-16"),
        "jakarta": (106.82, -6.18, 106.83, -6.17, "2022-11-21", "2022-11-30"),
    }
    west, south, east, north, start, end = fixtures[args.case]
    # Public imagery verification does not need credentials, a database, or EE.
    area = {"type": "Polygon", "coordinates": [[[west, south],
        [east, south], [east, north], [west, north], [west, south]]]}
    started = time.monotonic()
    response = imagery.list_scenes({"satellite": "vantor_open_data", "aoi": area,
        "start_date": start, "end_date": end})
    target = Path(__file__).resolve().parents[1] / "var" / "maxar-verification"
    if args.case != "cianjur":
        target = target / args.case
    target.mkdir(parents=True, exist_ok=True)
    if not response["scenes"]:
        report = {"case": args.case, "aoi": area, "start_date": start,
            "end_date": end, "scene_count": 0, "browser_verified": False,
            "seconds": round(time.monotonic()-started, 2)}
        (target / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
        return
    scene = response["scenes"][0]
    box = scene["bbox"]
    lon, lat = (box[0]+box[2])/2, (box[1]+box[3])/2
    z = 17
    x = int((lon+180)/360 * 2**z)
    y = int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2 * 2**z)
    png = imagery.render_maxar_open_data_tile(scene["id"], z, x, y)
    if not png:
        raise RuntimeError("Maxar renderer returned no pixels")
    pixels = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    visible = pixels[:, :, 3] > 0
    if not visible.any() or float(pixels[:, :, :3][visible].std()) <= 1:
        raise RuntimeError("Maxar tile is transparent or blank")
    (target / "scene.png").write_bytes(png)
    report = {"provider": "vantor_open_data", "case": args.case, "aoi": area,
        "start_date": start, "end_date": end,
        "scene_count": response["count"], "scene": scene,
        "tile": {"z": z, "x": x, "y": y, "png_bytes": len(png),
            "visible_pixels": int(visible.sum()), "pixel_std": float(pixels[:, :, :3][visible].std())},
        "seconds": round(time.monotonic()-started, 2), "browser_verified": False}
    (target / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
