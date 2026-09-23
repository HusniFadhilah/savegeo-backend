"""Audit and optionally re-run persisted flood products.

Default mode is report-only.  The script only creates a run when an actual
Sentinel-1/Sentinel-2-compatible pair is already present in the database; it
never substitutes BlackSky for Sentinel-1 or turns a missing scene into zero.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.registries.disaster_model_registry import list_models
from app.repositories import disaster_repo
from app.services.disaster_analysis_service import run_analysis
from app.services.disaster_capability_service import check_inputs


def _models():
    return [model for model in list_models(enabled_only=True, disaster_type="flood")]


def _event_report(db, event, create_runs: bool, execute: bool) -> dict:
    aoi = disaster_repo.get_active_aoi(db, event.id)
    pre = disaster_repo.list_imagery(db, event.id, "pre")
    post = disaster_repo.list_imagery(db, event.id, "post")
    models = []
    for model in _models():
        pair = None
        reasons = []
        for before in pre:
            for after in post:
                check = check_inputs(model["model_id"], event, aoi, before, after)
                if check.allowed:
                    pair = (before, after)
                    break
                reasons.extend(check.reasons)
            if pair:
                break
        item = {
            "model_id": model["model_id"],
            "status": "ready" if pair else "not_available",
            "reasons": [] if pair else sorted(set(reasons))[:8] or ["AOI atau imagery pre/post belum tersedia"],
        }
        if pair and create_runs:
            before, after = pair
            existing = next(
                (
                    run for run in disaster_repo.list_runs_for_event(db, event.id)
                    if run.model_id == model["model_id"]
                    and run.aoi_id == aoi.id
                    and run.pre_imagery_id == before.id
                    and run.post_imagery_id == after.id
                    and run.status not in {"failed", "stale"}
                ),
                None,
            )
            if existing:
                item["run_id"] = existing.id
                item["run_status"] = existing.status
            else:
                run = disaster_repo.create_run(
                    db,
                    event.id,
                    {
                        "model_id": model["model_id"],
                        "model_version": model["version"],
                        "aoi_id": aoi.id,
                        "pre_imagery_id": before.id,
                        "post_imagery_id": after.id,
                        "parameters": {"workflow": "flood_reprocess", "source": "reprocess_flood_results.py"},
                    },
                    None,
                )
                item["run_id"] = run.id
                item["run_status"] = run.status
                if execute:
                    outcome = run_analysis(db, run.id, force=True)
                    item["run_status"] = outcome["run"]["status"]
        models.append(item)
    return {
        "event_id": event.id,
        "name": event.name,
        "status": event.status,
        "aoi_id": aoi.id if aoi else None,
        "imagery": {"pre": len(pre), "post": len(post)},
        "models": models,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--create-runs", action="store_true", help="Create runs only for compatible existing inputs")
    parser.add_argument("--execute", action="store_true", help="Execute newly created compatible runs")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.execute and not args.create_runs:
        parser.error("--execute memerlukan --create-runs")

    db = SessionLocal()
    try:
        events = [disaster_repo.get_event(db, args.event_id)] if args.event_id else disaster_repo.list_events(
            db, published_only=False, disaster_type="flood"
        )
        reports = [_event_report(db, event, args.create_runs, args.execute) for event in events if event]
    finally:
        db.close()
    payload = {"mode": "execute" if args.execute else "create_runs" if args.create_runs else "report_only", "events": reports}
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
