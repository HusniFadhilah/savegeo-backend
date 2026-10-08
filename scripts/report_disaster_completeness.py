"""Read-only inventory of every disaster event and persisted analysis."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.models.disaster_event import DisasterEvent
from app.db.session import SessionLocal
from app.registries.disaster_model_registry import list_models
from app.repositories import disaster_repo
from app.services.disaster_capability_service import validate_persisted_result
from app.services.disaster_completeness_service import build_completeness
from app.services.samgeo_service import capabilities


def build_report(db):
    events = []
    for event in db.scalars(select(DisasterEvent).order_by(DisasterEvent.id)):
        imagery = disaster_repo.list_imagery(db, event.id)
        analyses = []
        for run in disaster_repo.list_runs_for_event(db, event.id):
            result = disaster_repo.get_result_for_run(db, run.id)
            check = validate_persisted_result(db, event, run, result) if result else None
            analyses.append({
                "run_id": run.id, "model_id": run.model_id, "status": run.status,
                "published": bool(result and result.is_published),
                "usable": bool(check and check.allowed),
                "reasons": list(check.reasons) if check else ["Hasil belum tersedia"],
                "statistics": result.to_dict()["statistics"] if result else None,
                "comparison": (result.statistics or {}).get("comparison") if result else None,
                "generated_at": (result.provenance or {}).get("generated_at") if result else None,
                "error": run.error_message,
            })
        aoi = disaster_repo.get_active_aoi(db, event.id)
        entries = []
        for analysis in analyses:
            model = next((m for m in list_models() if m["model_id"] == analysis["model_id"]), {})
            entries.append({**model, "available": analysis["usable"], "result": {"comparison": analysis["comparison"]}})
        events.append({
            "id": event.id, "name": event.name, "type": event.disaster_type,
            "date": event.event_date.isoformat() if event.event_date else None,
            "status": event.status, "is_public": event.is_public, "source": event.source,
            "aoi": aoi.to_dict() if aoi else None,
            "imagery": [i.to_dict() for i in imagery], "analyses": analyses,
            "completeness": build_completeness(event.disaster_type, entries),
        })
    return {"generated_at": dt.datetime.now(dt.UTC).isoformat(), "mode": "read_only",
            "counts": dict(Counter(e["type"] for e in events)), "events": events,
            "models": list_models(), "segmentation_runtime": capabilities()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        report = build_report(db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"counts": report["counts"], "events": [{
        "id": e["id"], "name": e["name"], "type": e["type"], "status": e["status"],
        "imagery": len(e["imagery"]), "analyses": len(e["analyses"]),
        "usable": [a["model_id"] for a in e["analyses"] if a["usable"]],
    } for e in report["events"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
