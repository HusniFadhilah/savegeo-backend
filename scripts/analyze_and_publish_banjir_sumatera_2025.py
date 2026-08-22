"""Second pass for "Banjir Sumatera 2025": for every draft DisasterEvent that
`ingest_banjir_sumatera_2025.py` created, run the 3 real analysis models
(flood_change_v1, water_segmentation_v1, forest_change_v1) against that
event's AOI + pre/post imagery, then publish whichever results actually
completed and flip the event to "published" if at least one did.

Kept as a separate script (not folded into ingestion) so a location whose
COG conversion/imagery registration succeeded but whose Sentinel query fails
(e.g. no cloud-free Sentinel-2 in the pre-flood window) can be re-run here
without re-touching the raster/AOI/imagery rows.

Run from savegeo/backend with its venv active, AFTER ingest_banjir_sumatera_2025.py:
    python scripts/analyze_and_publish_banjir_sumatera_2025.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

MODEL_IDS = ["flood_change_v1", "water_segmentation_v1", "forest_change_v1"]


def process_event(db, event) -> None:
    from app.repositories import disaster_repo
    from app.services import disaster_analysis_service
    from app.services.gee_common import AnalysisError

    print(f"\n=== event id={event.id} '{event.name}' ===")
    aoi = disaster_repo.get_active_aoi(db, event.id)
    pre_img = disaster_repo.get_primary_imagery(db, event.id, "pre")
    post_img = disaster_repo.get_primary_imagery(db, event.id, "post")
    if aoi is None:
        print("  SKIP: no AOI")
        return
    print(f"  AOI id={aoi.id} pre_imagery={pre_img.id if pre_img else None} post_imagery={post_img.id if post_img else None}")

    any_published = False
    for model_id in MODEL_IDS:
        run = disaster_repo.create_run(
            db,
            event.id,
            {
                "model_id": model_id,
                "aoi_id": aoi.id,
                "pre_imagery_id": pre_img.id if pre_img else None,
                "post_imagery_id": post_img.id if post_img else None,
            },
            created_by=None,
        )
        try:
            disaster_analysis_service.run_analysis(db, run.id)
        except AnalysisError as e:
            print(f"  {model_id}: FAILED ({e})")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  {model_id}: FAILED unexpected ({e})")
            continue

        result = disaster_repo.get_result_for_run(db, run.id)
        if result is None:
            print(f"  {model_id}: completed but no result row - skipping publish")
            continue
        disaster_repo.publish_result(db, result, published_by=None)
        any_published = True
        print(f"  {model_id}: OK, published (result id={result.id})")

    if any_published:
        event.status = "published"
        db.commit()
        print(f"  event {event.id} -> status=published")
    else:
        print(f"  event {event.id}: no analysis succeeded - left as '{event.status}', not published")


def main() -> None:
    from app.db.models.disaster_event import DisasterEvent
    from app.db.session import SessionLocal
    from app.services.gee_service import initialize_ee

    with SessionLocal() as db:
        if not initialize_ee(db):
            print("WARNING: Earth Engine did not initialize - every analysis run will fail.")
        events = (
            db.execute(
                select(DisasterEvent)
                .where(DisasterEvent.name.like("Banjir Sumatera 2025 - %"))
                .where(DisasterEvent.status == "draft")
                .order_by(DisasterEvent.id)
            )
            .scalars()
            .all()
        )
        print(f"Found {len(events)} draft event(s) to process.")
        for event in events:
            try:
                process_event(db, event)
            except Exception as e:  # noqa: BLE001
                print(f"  FAILED (event {event.id}): {e}")


if __name__ == "__main__":
    main()
