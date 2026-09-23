"""Revalidate persisted disaster results against the current registry.

The default is report-only.  Use ``--unpublish`` only after reviewing the
JSON report; invalid published results are marked unpublished and their run is
marked ``stale``.  No result or imagery row is deleted.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db.models.analysis_result import AnalysisResult
from app.db.models.analysis_run import AnalysisRun
from app.db.models.disaster_event import DisasterEvent
from app.db.session import SessionLocal
from app.services.disaster_capability_service import validate_persisted_result
from sqlalchemy import select


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unpublish", action="store_true", help="Unpublish invalid results and mark runs stale.")
    parser.add_argument("--output", type=Path, default=Path("disaster_result_revalidation.json"))
    args = parser.parse_args()
    findings = []
    with SessionLocal() as db:
        rows = db.execute(
            select(DisasterEvent, AnalysisRun, AnalysisResult)
            .join(AnalysisRun, AnalysisRun.event_id == DisasterEvent.id)
            .join(AnalysisResult, AnalysisResult.run_id == AnalysisRun.id)
            .where(AnalysisResult.is_published.is_(True))
        ).all()
        for event, run, result in rows:
            check = validate_persisted_result(db, event, run, result)
            if check.allowed:
                continue
            finding = {
                "event_id": event.id, "run_id": run.id, "result_id": result.id,
                "model_id": run.model_id, "status": check.status, "reasons": list(check.reasons),
            }
            findings.append(finding)
            if args.unpublish:
                result.is_published = False
                result.stale_reason = "; ".join(check.reasons)
                run.status = "stale"
        if args.unpublish and findings:
            db.commit()
    payload = {"revalidated": len(findings), "unpublished": len(findings) if args.unpublish else 0, "findings": findings}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
