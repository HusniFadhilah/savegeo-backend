"""Opt-in real GEE verification; does not create or publish disaster records.

Run from backend with configured GEE credentials. The output contains ephemeral
tile URLs and belongs in ignored frontend/test-results, never in a commit.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ee
from app.core.config import get_settings
from app.services.disaster_segmentation_service import compute_segmentation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pre", type=dt.date.fromisoformat, default=dt.date(2024, 6, 15))
    parser.add_argument("--post", type=dt.date.fromisoformat, default=dt.date(2025, 8, 4))
    parser.add_argument("--bbox", nargs=4, type=float, default=[115.13, -8.72, 115.28, -8.55])
    parser.add_argument("--output", type=Path, default=Path("../frontend/test-results/segmentation/live.json"))
    args = parser.parse_args()
    if args.pre >= args.post:
        parser.error("pre must precede post")
    settings = get_settings()
    ee.Initialize(ee.ServiceAccountCredentials(settings.gee_service_account, settings.gee_key_file), project=settings.gee_project_id)
    result = compute_segmentation(ee.Geometry.Rectangle(args.bbox), args.pre, args.post)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result), encoding="utf-8")
    comparison = result["statistics"]["comparison"]
    print(json.dumps({key: comparison[key] for key in ("pre", "post", "classes", "aoi_area_ha", "valid_area_ha", "changed_area_ha", "review_reasons")}, indent=2))


if __name__ == "__main__":
    main()
