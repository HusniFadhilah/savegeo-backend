"""Audit the disaster catalog without changing data by default.

Examples (run from ``backend``):

    python scripts/audit_disaster_catalog.py --report-only
    python scripts/audit_disaster_catalog.py --fix-public-flags
    python scripts/audit_disaster_catalog.py --unpublish-invalid-results --strict

Every mutating option is opt-in.  The audit intentionally reports old results
without the new provenance contract as stale instead of silently repairing or
relabeling them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, inspect, select, update

from app.db.models.analysis_result import AnalysisResult
from app.db.models.analysis_run import AnalysisRun
from app.db.models.disaster_aoi import DisasterAOI
from app.db.models.disaster_event import DisasterEvent
from app.db.models.hotspot import Hotspot
from app.db.models.satellite_imagery import SatelliteImagery
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.db.session import SessionLocal
from app.registries.disaster_model_registry import get_model
from app.services.disaster_capability_service import check_inputs, validate_persisted_result
from app.repositories import disaster_repo


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _fingerprint(event: DisasterEvent, aoi: DisasterAOI | None) -> tuple:
    centroid = (aoi.centroid or {}) if aoi else {}
    center = (
        event.center_lat if event.center_lat is not None else centroid.get("lat"),
        event.center_lon if event.center_lon is not None else centroid.get("lng"),
    )
    provinces = tuple(sorted(_norm(p) for p in (event.province or [])))
    return (
        _norm(event.slug), _norm(event.name), event.event_date.isoformat() if event.event_date else None,
        provinces, tuple(round(float(v), 4) if v is not None else None for v in center),
    )


def _event_dict(event: DisasterEvent, aoi: DisasterAOI | None, imagery: list[SatelliteImagery], runs: list[AnalysisRun]) -> dict:
    return {
        "id": event.id, "name": event.name, "slug": event.slug, "disaster_type": event.disaster_type,
        "status": event.status, "is_public": event.is_public,
        "location_name": event.location_name, "province": event.province or [],
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "aoi_id": aoi.id if aoi else None,
        "imagery": [{"id": i.id, "phase": i.phase, "satellite": i.satellite, "sensor": i.sensor,
                      "source_kind": i.source_kind, "acquisition_date": i.acquisition_date.isoformat() if i.acquisition_date else None}
                     for i in imagery],
        "analysis_run_count": len(runs),
    }


def _table(title: str, rows: list[dict], columns: list[str]) -> None:
    print(f"\n{title} ({len(rows)})")
    if not rows:
        print("  none")
        return
    widths = {column: max(len(column), *(len(str(row.get(column, ""))) for row in rows)) for column in columns}
    print("  " + " | ".join(column.ljust(widths[column]) for column in columns))
    print("  " + "-+-".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  " + " | ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def build_report(db) -> tuple[dict[str, Any], list[dict], dict[int, DisasterAOI | None], dict[int, list[AnalysisRun]]]:
    events = list(db.execute(select(DisasterEvent).order_by(DisasterEvent.id)).scalars())
    aois = {}
    for aoi in db.execute(select(DisasterAOI).order_by(DisasterAOI.created_at.desc())).scalars():
        aois.setdefault(aoi.event_id, aoi)
    imagery = defaultdict(list)
    for row in db.execute(select(SatelliteImagery)).scalars():
        imagery[row.event_id].append(row)
    runs = defaultdict(list)
    for row in db.execute(select(AnalysisRun)).scalars():
        runs[row.event_id].append(row)
    result_columns = {column["name"] for column in inspect(db.bind).get_columns("analysis_results")}
    result_field_names = (
        "id", "run_id", "tile_url", "statistics", "features", "legend", "confidence_summary",
        "is_published", "published_at", "published_by", "publication_version", "created_at",
    )
    result_fields = [getattr(AnalysisResult, name) for name in result_field_names if name in result_columns]
    if not result_fields:
        result_rows = []
    else:
        result_rows = db.execute(select(*result_fields)).mappings().all()
    results = {
        row["run_id"]: SimpleNamespace(**{name: row.get(name) for name in result_field_names},
                                        provenance=None, validation_status=None, limitations=None, stale_reason=None)
        for row in result_rows
    }

    event_counts = Counter((event.disaster_type, event.status) for event in events)
    published_not_public = [
        {"id": e.id, "name": e.name, "status": e.status, "is_public": e.is_public}
        for e in events if e.status == "published" and not e.is_public
    ]
    drafts = [_event_dict(e, aois.get(e.id), imagery[e.id], runs[e.id]) for e in events if e.status == "draft"]

    groups: dict[tuple, list[DisasterEvent]] = defaultdict(list)
    for event in events:
        groups[_fingerprint(event, aois.get(event.id))].append(event)
    duplicate_candidates = []
    for fingerprint, group in groups.items():
        published = [e for e in group if e.status == "published"]
        drafts_in_group = [e for e in group if e.status == "draft"]
        if published and drafts_in_group:
            primary = sorted(published, key=lambda e: e.id)[0]
            for draft in drafts_in_group:
                duplicate_candidates.append({
                    "candidate_id": draft.id, "candidate_name": draft.name,
                    "published_match_id": primary.id, "published_match_name": primary.name,
                    "reason": "same slug/name/date/province/centroid fingerprint",
                    "fingerprint": list(fingerprint),
                })
        elif len(group) > 1:
            duplicate_candidates.extend({
                "candidate_id": e.id, "candidate_name": e.name,
                "published_match_id": None, "published_match_name": None,
                "reason": "same catalog fingerprint; manual review required",
                "fingerprint": list(fingerprint),
            } for e in group)

    invalid_results = []
    incompatible_runs = []
    runs_without_result = []
    results_without_provenance = []
    for event in events:
        for run in runs[event.id]:
            result = results.get(run.id)
            if result is None:
                runs_without_result.append({"run_id": run.id, "event_id": event.id, "model_id": run.model_id, "status": run.status})
            if result is not None and not result.provenance:
                results_without_provenance.append({"result_id": result.id, "run_id": run.id, "event_id": event.id, "model_id": run.model_id})
            aoi = disaster_repo.get_aoi(db, run.aoi_id)
            pre = disaster_repo.get_imagery(db, run.pre_imagery_id) if run.pre_imagery_id else None
            post = disaster_repo.get_imagery(db, run.post_imagery_id) if run.post_imagery_id else None
            check = check_inputs(run.model_id, event, aoi, pre, post)
            if not check.allowed:
                incompatible_runs.append({"run_id": run.id, "event_id": event.id, "model_id": run.model_id,
                                          "status": run.status, "reasons": list(check.reasons)})
            if result is not None and result.is_published:
                check = validate_persisted_result(db, event, run, result)
                if not check.allowed:
                    invalid_results.append({"result_id": result.id, "run_id": run.id, "event_id": event.id,
                                            "model_id": run.model_id, "status": check.status,
                                            "reasons": list(check.reasons)})

    published_without_aoi = [{"id": e.id, "name": e.name} for e in events if e.status == "published" and not aois.get(e.id)]
    published_without_result = [{"id": e.id, "name": e.name} for e in events if e.status == "published" and not any(
        results.get(run.id) and results[run.id].statistics for run in runs[e.id]
    )]
    forest_fire_without_source = []
    earthquakes_without_damage_model = []
    for event in events:
        if event.disaster_type == "forest_fire":
            hotspot_count = db.scalar(select(func.count(WildfireHotspot.id)).where(WildfireHotspot.event_id == event.id))
            curated_count = db.scalar(select(func.count(Hotspot.id)).where(Hotspot.event_id == event.id))
            if not hotspot_count and not curated_count and not (event.hotspot_dataset_ids or event.burned_area_dataset_ids):
                forest_fire_without_source.append({"id": event.id, "name": event.name})
        if event.disaster_type == "earthquake":
            has_damage = any(
                get_model(run.model_id) and get_model(run.model_id).get("damage_model")
                and run.status in {"completed", "review_required", "published"}
                for run in runs[event.id]
            )
            if not has_damage:
                earthquakes_without_damage_model.append({"id": event.id, "name": event.name})

    report = {
        "report_version": "1.0",
        "event_counts": [{"disaster_type": t, "status": s, "count": count} for (t, s), count in sorted(event_counts.items())],
        "published_not_public": published_not_public,
        "drafts": drafts,
        "duplicate_candidates": duplicate_candidates,
        "published_results_with_incompatible_or_stale_inputs": invalid_results,
        "runs_without_result": runs_without_result,
        "results_without_provenance": results_without_provenance,
        "incompatible_runs": incompatible_runs,
        "published_events_without_aoi": published_without_aoi,
        "published_events_without_analysis_result": published_without_result,
        "forest_fire_without_hotspot_or_burned_area_source": forest_fire_without_source,
        "earthquake_without_valid_damage_model": earthquakes_without_damage_model,
        "schema_missing_new_result_columns": sorted({"provenance", "validation_status", "limitations", "stale_reason"}.difference(result_columns)),
    }
    return report, duplicate_candidates, aois, runs


def apply_fixes(db, report: dict, *, archive_duplicates: bool, fix_public_flags: bool,
                unpublish_invalid: bool, mark_stale_runs: bool) -> dict:
    changes = {"public_flags_fixed": [], "duplicates_archived": [], "invalid_results_unpublished": [], "runs_marked_stale": []}
    if fix_public_flags:
        for row in db.execute(select(DisasterEvent)).scalars():
            expected = row.status == "published"
            if row.is_public != expected:
                changes["public_flags_fixed"].append(row.id)
                row.is_public = expected
        db.commit()
    if archive_duplicates:
        candidate_ids = {row["candidate_id"] for row in report["duplicate_candidates"] if row["published_match_id"] is not None}
        for row in db.execute(select(DisasterEvent).where(DisasterEvent.id.in_(candidate_ids))).scalars() if candidate_ids else []:
            if row.status == "draft":
                row.status, row.is_public = "archived", False
                changes["duplicates_archived"].append(row.id)
        db.commit()
    if unpublish_invalid:
        result_ids = {row["result_id"] for row in report["published_results_with_incompatible_or_stale_inputs"]}
        if result_ids:
            values = {"is_published": False}
            if "stale_reason" not in report.get("schema_missing_new_result_columns", []):
                values["stale_reason"] = "Audit capability check failed; revalidation required"
            db.execute(update(AnalysisResult).where(AnalysisResult.id.in_(result_ids)).values(**values))
            for run in db.execute(select(AnalysisRun).where(AnalysisRun.id.in_(
                select(AnalysisResult.run_id).where(AnalysisResult.id.in_(result_ids))
            ))).scalars():
                if run.status == "published":
                    run.status = "stale"
            changes["invalid_results_unpublished"] = sorted(result_ids)
        db.commit()
    if mark_stale_runs:
        stale_runs = db.execute(
            select(AnalysisRun)
            .join(AnalysisResult, AnalysisResult.run_id == AnalysisRun.id)
            .where(
                AnalysisResult.stale_reason.is_not(None),
                AnalysisRun.status.in_(["completed", "review_required", "published"]),
            )
        ).scalars().all()
        for run in stale_runs:
            run.status = "stale"
            changes["runs_marked_stale"].append(run.id)
        db.commit()
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-only", action="store_true", help="Explicit no-write mode (default).")
    parser.add_argument("--strict", action="store_true", help="Exit 2 when any audit finding exists.")
    parser.add_argument("--archive-duplicates", action="store_true")
    parser.add_argument("--fix-public-flags", action="store_true")
    parser.add_argument("--unpublish-invalid-results", action="store_true")
    parser.add_argument("--mark-stale-runs", action="store_true", help="Mark runs linked to stale results as stale.")
    parser.add_argument("--output", type=Path, default=Path("disaster_catalog_audit.json"))
    args = parser.parse_args()
    if args.report_only and any((args.archive_duplicates, args.fix_public_flags, args.unpublish_invalid_results, args.mark_stale_runs)):
        parser.error("--report-only tidak dapat digabung dengan flag perubahan database")

    with SessionLocal() as db:
        report, _duplicates, _aois, _runs = build_report(db)
        changes = apply_fixes(
            db, report, archive_duplicates=args.archive_duplicates,
            fix_public_flags=args.fix_public_flags, unpublish_invalid=args.unpublish_invalid_results,
            mark_stale_runs=args.mark_stale_runs,
        ) if any((args.archive_duplicates, args.fix_public_flags, args.unpublish_invalid_results, args.mark_stale_runs)) else {}
    if changes:
        report["changes_applied"] = changes
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    _table("Event counts", report["event_counts"], ["disaster_type", "status", "count"])
    for key in (
        "published_not_public", "duplicate_candidates", "published_results_with_incompatible_or_stale_inputs",
        "runs_without_result", "results_without_provenance", "incompatible_runs", "published_events_without_aoi",
        "published_events_without_analysis_result", "forest_fire_without_hotspot_or_burned_area_source",
        "earthquake_without_valid_damage_model",
    ):
        rows = report[key]
        columns = list(rows[0].keys())[:4] if rows else ["finding"]
        _table(key, rows, columns)
    print(f"\nJSON report: {args.output}")
    finding_count = sum(len(value) for key, value in report.items() if key != "event_counts" and isinstance(value, list))
    return 2 if args.strict and finding_count else 0


if __name__ == "__main__":
    sys.exit(main())
