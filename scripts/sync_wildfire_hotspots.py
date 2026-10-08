"""Explicitly fill the persistent wildfire cache without blocking map reads."""

import argparse
import json
from datetime import date

from sqlalchemy import select

from app.db.models.disaster_event import DisasterEvent
from app.db.session import SessionLocal
from app.services.wildfire_hotspot_service import sync_event_hotspots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--include-unpublished", action="store_true", help="Prepare draft/ready-for-review event data before verified publication")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--to must not be earlier than --from")
    with SessionLocal() as db:
        event = db.execute(select(DisasterEvent).where(
            DisasterEvent.slug == args.slug, DisasterEvent.disaster_type == "forest_fire",
            DisasterEvent.status.in_(["published", "draft", "ready_for_review"] if args.include_unpublished else ["published"]),
        )).scalar_one()
        print(f"Syncing {event.slug}: {args.start} to {args.end}", flush=True)
        result = sync_event_hotspots(db, event, args.start, args.end)
        print(json.dumps(result, default=str), flush=True)
        if result.get("errors"):
            parser.exit(1, "FIRMS synchronization was incomplete; inspect the reported source errors.\n")


if __name__ == "__main__":
    main()
