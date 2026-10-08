"""Discover genuine Sentinel scenes and optionally compute published indicators.

Existing primary imagery and legacy runs are preserved. New dates and scene IDs
are recorded explicitly. Building damage is never inferred by this operation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ee
import requests
from PIL import Image

from app.db.session import SessionLocal
from app.registries.disaster_model_registry import get_model
from app.repositories import disaster_repo as repo
from app.services import gee_service
from app.services.disaster_analysis_service import run_analysis
from app.services.disaster_capability_service import validate_persisted_result
from app.services.gee_common import create_geometry_from_payload


def scenes(collection, aoi, start, end):
    col = ee.ImageCollection(collection).filterBounds(aoi).filterDate(start.isoformat(), end.isoformat())
    if collection.endswith("S1_GRD"):
        col = col.filter(ee.Filter.eq("instrumentMode", "IW")).filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    fields = ["system:index", "system:time_start", "orbitProperties_pass", "relativeOrbitNumber_start"]
    if collection.endswith("DYNAMICWORLD/V1"):
        def with_coverage(image):
            valid = image.select("label").mask().unmask(0, False).rename("valid")
            coverage = valid.reduceRegion(ee.Reducer.mean(), aoi, 100, crs=image.select("label").projection(), maxPixels=10_000_000).get("valid")
            return image.set("candidate_coverage", coverage)
        col = col.map(with_coverage)
        fields.append("candidate_coverage")
    items = col.map(lambda im: im.set("audit", im.toDictionary(fields))).aggregate_array("audit").getInfo()
    return [{**i, "date": dt.datetime.fromtimestamp(i["system:time_start"]/1000, dt.UTC).date()} for i in items]


def choose_pairs(before, after, *, sar):
    pairs = [(p, q) for p in before for q in after if p["date"] < q["date"] and (not sar or all(
        p.get(k) is not None and p.get(k) == q.get(k) for k in ("orbitProperties_pass", "relativeOrbitNumber_start")))]
    return sorted(pairs, key=lambda pq: (
        0 if sar else -min(pq[0].get("candidate_coverage") or 0, pq[1].get("candidate_coverage") or 0),
        (pq[1]["date"]-pq[0]["date"]).days, pq[1]["date"], pq[0]["system:index"], pq[1]["system:index"]))


def tile_check(url, aoi_row):
    lon = (aoi_row.bbox[0]+aoi_row.bbox[2])/2
    lat = (aoi_row.bbox[1]+aoi_row.bbox[3])/2
    zoom = 12
    x = int((lon+180)/360*2**zoom)
    y = int((1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*2**zoom)
    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        response = requests.get(url.replace("{z}", str(zoom)).replace("{x}", str(x+dx)).replace("{y}", str(y+dy)), timeout=60)
        if response.status_code != 200:
            continue
        image = Image.open(io.BytesIO(response.content)).convert("RGBA")
        if image.getchannel("A").getextrema()[1] > 0:
            return {"status": 200, "zoom": zoom, "x": x+dx, "y": y+dy, "visible_pixels": True}
    raise ValueError("Tidak ada tile citra non-transparan yang berhasil dimuat pada AOI")


def register_scene(db, event, phase, scene, *, sar):
    collection = "COPERNICUS/S1_GRD" if sar else "GOOGLE/DYNAMICWORLD/V1"
    return repo.add_imagery(db, event.id, {
        "phase": phase, "satellite": "Sentinel-1" if sar else "Sentinel-2",
        "sensor": "SAR VV" if sar else "MSI", "acquisition_date": scene["date"],
        "resolution_m": 10, "source_kind": "gee", "scene_id": scene["system:index"],
        "data_source": collection, "is_primary": False,
    })


def compute_product(db, event, aoi_row, pairs, model_id, args):
    sar = model_id == "flood_change_v2"
    report = {"model_id": model_id, "candidate_pair_count": len(pairs), "attempts": [], "status": "missing_data"}
    if not pairs:
        report["reason"] = "Tidak ada pasangan scene kompatibel pada rentang yang dinyatakan"
        return report
    if not args.execute:
        report.update(status="ready_to_compute", selected=pairs[0])
        return report
    for before, after in pairs[:args.max_pairs]:
        item = {"pre_scene_id": before["system:index"], "post_scene_id": after["system:index"],
                "pre_date": before["date"], "post_date": after["date"]}
        report["attempts"].append(item)
        try:
            pre = register_scene(db, event, "pre", before, sar=sar)
            post = register_scene(db, event, "post", after, sar=sar)
            parameters = {"workflow": "disaster_enrichment_v1", "selection": "highest valid coverage, then nearest compatible acquisition pair",
                          "pre_window_days": args.pre_days, "post_window_days": args.post_days,
                          "interpretation": "Indikator pada tanggal citra; bukan puncak bencana atau kerusakan bangunan"}
            run = next((r for r in repo.list_runs_for_event(db, event.id) if r.model_id == model_id and
                        r.aoi_id == aoi_row.id and r.pre_imagery_id == pre.id and r.post_imagery_id == post.id and
                        r.parameters == parameters and r.status in {"completed", "review_required", "published"}), None)
            if run is None:
                run = repo.create_run(db, event.id, {"model_id": model_id, "model_version": get_model(model_id)["version"],
                    "aoi_id": aoi_row.id, "pre_imagery_id": pre.id, "post_imagery_id": post.id, "parameters": parameters}, None)
            item["run_id"] = run.id
            outcome = run_analysis(db, run.id)
            result = repo.get_result_for_run(db, run.id)
            coverage = (result.coverage or {}).get("coverage_pct", 0)
            item["coverage_pct"] = coverage
            item["statistics"] = outcome["result"]["statistics"]
            item["review_reasons"] = result.review_reasons
            if coverage < 50:
                item["status"] = "insufficient_coverage"
                continue
            check = validate_persisted_result(db, event, run, result, require_published=False)
            if not check.allowed:
                raise ValueError("; ".join(check.reasons))
            # Validate the actual pre and post segmentation raster, not just a footprint.
            comparison = (result.statistics or {}).get("comparison")
            urls = [comparison[k] for k in ("pre_tile_url", "post_tile_url")] if comparison else [result.tile_url]
            item["tile_checks"] = [tile_check(url, aoi_row) for url in urls]
            if args.publish_indicators:
                repo.publish_result(db, result, None)
                run.status = "published"
                db.commit()
            item["status"] = "published_indicator" if result.is_published else "computed_for_review"
            report.update(status=item["status"], run_id=run.id, selected=item)
            return report
        except Exception as exc:
            db.rollback()
            item.update(status="failed", reason=str(exc))
    report["status"] = "blocked_by_quality_or_data"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--pre-days", type=int, default=60)
    parser.add_argument("--post-days", type=int, default=45)
    parser.add_argument("--max-pairs", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--publish-indicators", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.publish_indicators and not args.execute:
        parser.error("--publish-indicators requires --execute")
    if not (1 <= args.pre_days <= 90 and 1 <= args.post_days <= 60 and 1 <= args.max_pairs <= 5):
        parser.error("Window or attempt limit invalid")
    payload = {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "mode": "execute" if args.execute else "discovery",
               "pre_days": args.pre_days, "post_days": args.post_days, "events": [],
               "limitation": "Indikator tutupan lahan/genangan pada tanggal observasi. Tidak menghitung bangunan rusak."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with SessionLocal() as db:
        if not gee_service.initialize_ee(db):
            raise SystemExit("Earth Engine belum tersedia")
        ee.data.setDeadline(180_000)
        events = repo.list_events(db, published_only=True)
        for event in events:
            if event.disaster_type not in {"flood", "earthquake"} or (args.event_id and args.event_id != event.id):
                continue
            item = {"event_id": event.id, "name": event.name, "disaster_type": event.disaster_type, "products": []}
            payload["events"].append(item)
            print(f"Event {event.id}: {event.name}", flush=True)
            try:
                aoi_row = repo.get_active_aoi(db, event.id)
                if not aoi_row or not event.event_date:
                    raise ValueError("AOI atau tanggal kejadian belum tersedia")
                aoi = create_geometry_from_payload({"geojson": aoi_row.geojson})
                for model_id, collection in [("dynamic_world_v1", "GOOGLE/DYNAMICWORLD/V1")] + (
                    [("flood_change_v2", "COPERNICUS/S1_GRD")] if event.disaster_type == "flood" else []):
                    before = scenes(collection, aoi, event.event_date-dt.timedelta(days=args.pre_days), event.event_date)
                    after = scenes(collection, aoi, event.event_date, event.event_date+dt.timedelta(days=args.post_days+1))
                    pairs = choose_pairs(before, after, sar=model_id == "flood_change_v2")
                    product = compute_product(db, event, aoi_row, pairs, model_id, args)
                    item["products"].append(product)
                    print(f"  {model_id}: {product['status']}", flush=True)
                    save()
            except Exception as exc:
                db.rollback()
                item.update(status="failed", reason=str(exc))
                print(f"  Failed: {exc}", flush=True)
            save()
    save()


if __name__ == "__main__":
    main()
