"""Opt-in inference on a real published event raster; candidates stay unpublished."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.disaster_events import EventSegmentationRequest, get_event_segmentation, start_event_segmentation
from app.db.session import SessionLocal
from app.repositories import disaster_repo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=int, required=True)
    parser.add_argument("--class-label", choices=["building", "road", "water", "tree"], default="building")
    parser.add_argument("--confidence-threshold", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    viewer = SimpleNamespace(id=0)
    with SessionLocal() as db:
        image = next((i for i in disaster_repo.list_imagery(db, args.event_id, "post") if i.source_kind == "local_upload"), None)
        if image is None:
            raise SystemExit("Tidak ada raster resolusi tinggi post-event")
        try:
            job = start_event_segmentation(args.event_id, EventSegmentationRequest(imagery_id=image.id, class_label=args.class_label, center_crop_m=500, confidence_threshold=args.confidence_threshold), viewer, db)
            print(json.dumps(job), flush=True)
            deadline = time.monotonic()+1000
            while job["status"] == "running" and time.monotonic() < deadline:
                time.sleep(5)
                job = get_event_segmentation(args.event_id, job["job_id"], viewer, db)
            args.output.write_text(json.dumps({"event_id": args.event_id, "imagery": image.to_dict(), "job": job}, indent=2), encoding="utf-8")
            result = job.get("result") or {}
            print(json.dumps({"status": job["status"], "metadata": result.get("metadata"), "polygon_count": len(result.get("features", [])), "message": job.get("message")}, default=str), flush=True)
            return 0 if job["status"] == "complete" else 1
        except Exception as exc:
            args.output.write_text(json.dumps({"status": "failed", "reason": str(exc)}), encoding="utf-8")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
