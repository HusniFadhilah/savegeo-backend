"""Run the real, enabled disaster-analysis models against the 9 NTT
earthquake events created by ingest_ntt_earthquake.py.

Only 3 models are actually implemented (see disaster_model_registry.py):
`forest_change_v1` (NDVI change - vegetation/tutupan lahan disturbance,
relevant to landslide-triggered clearing), `water_segmentation_v1`/
`flood_change_v1` (NDWI/SAR water extent - relevant for the 2 port
locations given this event's tsunami component). No building/road-damage
model exists (see the integration prompt) - not attempted here.

Run from savegeo/backend with its venv active:
    python scripts/run_ntt_earthquake_analyses.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (event_id, label, run water_segmentation_v1 too?)
EVENTS = [
    (5, "Labuan Bajo", True),  # coastal town
    (6, "Pelabuhan Maurole", True),  # port
    (7, "Lambaleda", False),
    (8, "Desa Riung Cibal", False),
    (9, "Satar Punda Bar", False),
    (10, "Satar Punda Bar (2)", False),
    (11, "Tagol", False),
    (12, "Pelabuhan Laurentius Say", True),  # port
    (16, "Desa Liang Deruk", False),
]


def run_one_model(db, event_id: int, aoi_id: int, pre_id: int | None, post_id: int, model_id: str) -> None:
    from app.repositories import disaster_repo
    from app.services import disaster_analysis_service
    from app.services.gee_common import AnalysisError

    run = disaster_repo.create_run(
        db, event_id,
        {"model_id": model_id, "aoi_id": aoi_id, "pre_imagery_id": pre_id, "post_imagery_id": post_id},
        created_by=None,
    )
    try:
        disaster_analysis_service.run_analysis(db, run.id)
        print(f"    {model_id}: run_id={run.id} OK")
    except AnalysisError as e:
        print(f"    {model_id}: run_id={run.id} FAILED - {e}")
    except Exception as e:  # noqa: BLE001
        print(f"    {model_id}: run_id={run.id} FAILED (unexpected) - {e}")


def main() -> None:
    from app.db.session import SessionLocal
    from app.repositories import disaster_repo
    from app.services.gee_service import initialize_ee

    with SessionLocal() as db:
        if not initialize_ee(db):
            print("WARNING: Earth Engine did not initialize.")

        for event_id, label, run_water in EVENTS:
            print(f"=== {label} (event {event_id}) ===")
            aoi = disaster_repo.get_active_aoi(db, event_id)
            if aoi is None:
                print("  SKIP: no AOI")
                continue
            pre_list = disaster_repo.list_imagery(db, event_id, "pre")
            post_list = disaster_repo.list_imagery(db, event_id, "post")
            pre_id = pre_list[-1].id if pre_list else None
            post_id = post_list[-1].id if post_list else None
            if post_id is None:
                print("  SKIP: no post imagery")
                continue

            run_one_model(db, event_id, aoi.id, pre_id, post_id, "forest_change_v1")
            if run_water:
                run_one_model(db, event_id, aoi.id, pre_id, post_id, "water_segmentation_v1")


if __name__ == "__main__":
    main()
