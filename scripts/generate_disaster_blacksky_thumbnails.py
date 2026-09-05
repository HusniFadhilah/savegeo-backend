"""Generate event thumbnails from locally ingested BlackSky/BSG COG imagery.

Run from savegeo/backend:
    python scripts/generate_disaster_blacksky_thumbnails.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rio_tiler.io import Reader
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models.disaster_event import DisasterEvent
from app.db.models.satellite_imagery import SatelliteImagery
from app.db.session import SessionLocal
from app.services.local_imagery_tile_service import _stretch_bounds


EVENT_PREFIXES = ("Gempa Bumi NTT (Flores) 2026", "Banjir Sumatera 2025")


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "event"


def _thumbnail_url(filename: str) -> str:
    return f"/disaster-thumbnails/{filename}"


def render_thumbnail(local_file_path: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Reader(local_file_path) as reader:
        img = reader.preview(max_size=720)
    img.rescale(in_range=_stretch_bounds(local_file_path)[: img.data.shape[0]])
    output_path.write_bytes(img.render(img_format="PNG"))


def main() -> None:
    settings = get_settings()
    thumb_dir = settings.disaster_raster_path / "thumbnails"

    with SessionLocal() as db:
        stmt = (
            select(DisasterEvent, SatelliteImagery)
            .join(SatelliteImagery, SatelliteImagery.event_id == DisasterEvent.id)
            .where(SatelliteImagery.phase == "post")
            .where(SatelliteImagery.source_kind == "local_upload")
            .where(SatelliteImagery.local_file_path.is_not(None))
            .order_by(DisasterEvent.id)
        )
        rows = [
            (event, imagery)
            for event, imagery in db.execute(stmt).all()
            if event.name.startswith(EVENT_PREFIXES)
        ]

        print(f"Found {len(rows)} local BlackSky/BSG post-imagery rows")
        for event, imagery in rows:
            if not imagery.local_file_path or not Path(imagery.local_file_path).exists():
                print(f"SKIP event {event.id}: local file missing")
                continue

            filename = f"event_{event.id}_{_slug(event.name)}.png"
            output_path = thumb_dir / filename
            print(f"event {event.id}: {output_path.name}")
            render_thumbnail(imagery.local_file_path, output_path)
            event.thumbnail = _thumbnail_url(filename)

        db.commit()
        print("Done")


if __name__ == "__main__":
    main()
