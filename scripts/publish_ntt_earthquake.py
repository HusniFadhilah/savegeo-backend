"""Publish all 9 NTT earthquake DisasterEvents + their completed analysis
results to the public Pemetaan Bencana dashboard.

Two separate publish gates exist (verified against disaster_repo.py):
  1. DisasterEvent.status = "published" - gates the event/AOI/imagery itself
     (disaster_repo.list_events(published_only=True) filters on this).
  2. AnalysisResult.is_published = True - gates each individual analysis
     result separately (list_published_analyses/get_published_analysis).
Both are needed for a location's analysis findings to actually show up on
the public dashboard, not just the event/imagery.

Run from savegeo/backend with its venv active:
    python scripts/publish_ntt_earthquake.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EVENT_IDS = [5, 6, 7, 8, 9, 10, 11, 12, 16]


def main() -> None:
    from app.db.session import SessionLocal
    from app.repositories import disaster_repo

    with SessionLocal() as db:
        for event_id in EVENT_IDS:
            event = disaster_repo.get_event(db, event_id)
            if event is None:
                print(f"event {event_id}: NOT FOUND, skipped")
                continue

            disaster_repo.update_event(db, event, {"status": "published"})
            print(f"event {event_id} ({event.name}): status -> published")

            runs = disaster_repo.list_runs_for_event(db, event_id)
            for run in runs:
                result = disaster_repo.get_result_for_run(db, run.id)
                if result is None:
                    print(f"  run {run.id} ({run.model_id}): no result yet, skipped")
                    continue
                disaster_repo.publish_result(db, result, published_by=None)
                print(f"  run {run.id} ({run.model_id}): result published")


if __name__ == "__main__":
    main()
