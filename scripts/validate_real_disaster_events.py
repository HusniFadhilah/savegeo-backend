"""Validate published, real-world disaster records without fabricating results.

The registry currently contains Dynamic World land-cover inference and index
methods, not a trained disaster-damage model. This command therefore reports
whether a published event has a complete, auditable pre/post land-cover
comparison. It never creates an event, substitutes imagery dates, or marks a
land-cover change as burned, landslide, tsunami, or structural damage.

Run from ``backend`` with the production-like database configured::

    python scripts/validate_real_disaster_events.py
    python scripts/validate_real_disaster_events.py --strict

``--strict`` exits with status 1 when any requested type has no validated
published comparison, which keeps this check useful in deployment CI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REQUIRED_TYPES = ("landslide", "forest_fire", "tsunami")


def _is_http_url(value: str | None) -> bool:
    try:
        parsed = urlparse(value or "")
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _event_report(db, event, *, repo) -> dict:
    aoi = repo.get_active_aoi(db, event.id)
    imagery = {phase: repo.list_imagery(db, event.id, phase=phase) for phase in ("pre", "post")}
    primary = {phase: repo.get_primary_imagery(db, event.id, phase) for phase in ("pre", "post")}
    reasons: list[str] = []
    if not _is_http_url(event.source):
        reasons.append("Sumber kejadian resmi berupa URL belum dicatat")
    if aoi is None:
        reasons.append("AOI aktif belum tersedia")
    pre, post = primary["pre"], primary["post"]
    if pre is None or post is None:
        reasons.append("Citra primer pre dan post belum lengkap")
    else:
        if pre.acquisition_date >= post.acquisition_date:
            reasons.append("Tanggal citra pre harus lebih awal daripada post")
        for image, phase in ((pre, "pre"), (post, "post")):
            if image.source_kind != "gee":
                reasons.append(f"Citra {phase} bukan sumber GEE yang didukung Dynamic World")
            if (image.sensor or "").lower() != "sentinel2":
                reasons.append(f"Sensor {phase} harus Sentinel-2 untuk Dynamic World")
            if image.resolution_m != 10:
                reasons.append(f"Resolusi {phase} harus 10 m untuk Dynamic World")
    published = repo.get_published_analysis(db, event.id, "dynamic_world_v1")
    comparison = published[1].to_dict().get("comparison") if published else None
    if not comparison:
        reasons.append("Hasil Dynamic World pre/post yang dipublikasikan belum tersedia")
    elif not comparison.get("classes") or not comparison.get("pre") or not comparison.get("post"):
        reasons.append("Hasil yang dipublikasikan tidak memiliki kelas dan metadata pre/post lengkap")
    status = "validated_land_cover_comparison" if not reasons else "not_validated"
    return {
        "event_id": event.id,
        "name": event.name,
        "disaster_type": event.disaster_type,
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "source": event.source,
        "status": status,
        "model_id": "dynamic_world_v1",
        "damage_model": False,
        "interpretation": "Perubahan kelas tutupan lahan; bukan label kerusakan bencana.",
        "reasons": reasons,
        "imagery_counts": {phase: len(rows) for phase, rows in imagery.items()},
        "published_result": bool(published),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--types", nargs="+", choices=REQUIRED_TYPES, default=list(REQUIRED_TYPES))
    parser.add_argument("--strict", action="store_true", help="gagal bila setiap tipe belum memiliki validasi")
    args = parser.parse_args()

    from app.db.session import SessionLocal
    from app.repositories import disaster_repo

    with SessionLocal() as db:
        reports = []
        for disaster_type in args.types:
            events = disaster_repo.list_events(db, published_only=True, disaster_type=disaster_type)
            reports.extend(_event_report(db, event, repo=disaster_repo) for event in events)
    by_type = {
        disaster_type: [report for report in reports if report["disaster_type"] == disaster_type]
        for disaster_type in args.types
    }
    validated_types = {kind for kind, values in by_type.items() if any(r["status"] == "validated_land_cover_comparison" for r in values)}
    payload = {
        "model_id": "dynamic_world_v1",
        "damage_model": False,
        "requested_types": args.types,
        "validated_types": sorted(validated_types),
        "reports": reports,
        "note": "Tidak ada hasil yang dibuat atau dipublikasikan oleh skrip ini.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not args.strict or validated_types == set(args.types) else 1


if __name__ == "__main__":
    raise SystemExit(main())
