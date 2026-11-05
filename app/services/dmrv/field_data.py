"""dMRV field data: import preview/commit and read models.

An import never overwrites: a visit (plot + date) that already has tree
measurements rejects the incoming rows with ``visit_exists``. Atomic mode saves
nothing but the batch record when any row is invalid; partial mode saves the
valid rows and reports the rest. The audit event is written in the same
transaction as the data.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from typing import Any, Literal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.field_import import (
    FieldImportError,
    ImportOptions,
    ImportReport,
    RowIssue,
    read_table,
    suggest_mapping,
    validate_rows,
)
from app.services.dmrv.field_metrics import plot_density

MAX_ISSUES = 200


def _assess(
    db: Session, project_id: str, filename: str, data: bytes, mapping: dict[str, str] | None, options: ImportOptions
) -> tuple[list[str], list[dict[str, str]], dict[str, str], ImportReport, list[RowIssue], set[int]]:
    try:
        headers, rows = read_table(filename, data)
    except FieldImportError as exc:
        raise DmrvError(422, exc.code, str(exc)) from exc
    mapping = mapping or suggest_mapping(headers)
    report = validate_rows(rows, mapping, options)

    extra: list[RowIssue] = []
    rejected = set(report.error_rows)
    codes = {tree.plot_code for tree in report.trees}
    existing = set(
        db.execute(
            select(SurveyPlot.plot_code, SurveyVisit.visit_date)
            .join(SurveyVisit, SurveyVisit.plot_id == SurveyPlot.id)
            .where(
                SurveyPlot.project_id == project_id,
                SurveyPlot.plot_code.in_(codes),
                exists().where(TreeMeasurement.visit_id == SurveyVisit.id),
            )
        ).all()
    ) if codes else set()
    for tree in report.trees:
        if (tree.plot_code, tree.visit_date) in existing:
            rejected.add(tree.row)
            extra.append(RowIssue(
                tree.row, "error", "visit_exists",
                f"Plot {tree.plot_code} sudah memiliki pengukuran pada {tree.visit_date}; impor tidak menimpa data lama.",
            ))
    return headers, rows, mapping, report, extra, rejected


def _issues_out(issues: list[RowIssue]) -> list[dict[str, Any]]:
    return [asdict(issue) for issue in sorted(issues, key=lambda i: (i.row, i.level))[:MAX_ISSUES]]


def preview_import(
    db: Session, principal: Principal, project_id: str, filename: str, data: bytes,
    mapping: dict[str, str] | None, options: ImportOptions | None,
) -> dict[str, Any]:
    authorize(db, project_id, principal, "field.write")
    options = options or ImportOptions(height_unit="m")
    headers, rows, mapping, report, extra, _ = _assess(db, project_id, filename, data, mapping, options)
    issues = report.issues + extra
    return {
        "headers": headers,
        "suggested_mapping": mapping,
        "sample": rows[:5],
        "report": {
            "rows_total": report.rows_total,
            "errors": len([i for i in issues if i.level == "error"]),
            "warnings": len([i for i in issues if i.level == "warning"]),
            "issues": _issues_out(issues),
        },
    }


def commit_import(
    db: Session, principal: Principal, project_id: str, filename: str, data: bytes,
    mapping: dict[str, str] | None, options: ImportOptions, mode: Literal["atomic", "partial"],
    request_id: str | None,
) -> dict[str, Any]:
    authorize(db, project_id, principal, "field.write")
    _, _, _, report, extra, rejected = _assess(db, project_id, filename, data, mapping, options)
    issues = report.issues + extra
    sha256 = hashlib.sha256(data).hexdigest()
    batch = ImportBatch(
        id=str(uuid.uuid4()), project_id=project_id, filename=filename[:255], sha256=sha256,
        size_bytes=len(data), mode=mode, status="rejected", summary={}, created_by=principal.key,
    )
    imported = plots_created = visits_created = 0
    try:
        db.add(batch)
        db.flush()
        if mode == "partial" or not rejected:
            accepted = [tree for tree in report.trees if tree.row not in rejected]
            plots = {
                plot.plot_code: plot
                for plot in db.execute(
                    select(SurveyPlot).where(
                        SurveyPlot.project_id == project_id,
                        SurveyPlot.plot_code.in_({tree.plot_code for tree in accepted}),
                    )
                ).scalars()
            } if accepted else {}
            visits: dict[tuple[str, Any], SurveyVisit] = {}
            warned_plots: set[str] = set()
            for tree in accepted:
                plot = plots.get(tree.plot_code)
                if plot is None:
                    plot = SurveyPlot(
                        id=str(uuid.uuid4()), project_id=project_id, plot_code=tree.plot_code,
                        latitude=tree.latitude, longitude=tree.longitude, area_m2=tree.area_m2, area_basis=tree.area_basis,
                    )
                    db.add(plot)
                    plots[tree.plot_code] = plot
                    plots_created += 1
                else:
                    if plot.area_m2 is None and tree.area_m2 is not None:
                        plot.area_m2, plot.area_basis = tree.area_m2, tree.area_basis
                    elif tree.area_m2 is not None and plot.area_m2 != tree.area_m2 and tree.plot_code not in warned_plots:
                        warned_plots.add(tree.plot_code)
                        issues.append(RowIssue(
                            tree.row, "warning", "area_conflict",
                            f"Luas plot {tree.plot_code} tersimpan {plot.area_m2} m2; nilai impor {tree.area_m2} m2 diabaikan.",
                        ))
                    if plot.latitude is None and tree.latitude is not None:
                        plot.latitude, plot.longitude = tree.latitude, tree.longitude
                key = (tree.plot_code, tree.visit_date)
                visit = visits.get(key)
                if visit is None:
                    db.flush()
                    visit = db.execute(
                        select(SurveyVisit).where(SurveyVisit.plot_id == plot.id, SurveyVisit.visit_date == tree.visit_date)
                    ).scalar_one_or_none()
                    if visit is None:
                        visit = SurveyVisit(
                            id=str(uuid.uuid4()), plot_id=plot.id, visit_date=tree.visit_date,
                            date_precision=tree.date_precision, operator=tree.operator, batch_id=batch.id,
                        )
                        db.add(visit)
                        visits_created += 1
                    visits[key] = visit
                db.add(TreeMeasurement(
                    visit_id=visit.id, tree_code=tree.tree_code, species=tree.species,
                    circumference_cm=tree.circumference_cm, dbh_cm=tree.dbh_cm, height_m=tree.height_m,
                    biomass_kg=tree.biomass_kg, status=tree.status,
                    biomass_source="reported" if tree.biomass_kg is not None else None, qa_flags=list(tree.flags),
                ))
                imported += 1
        rows_rejected = len(rejected)
        status = "committed" if not rejected else ("partial" if imported else "rejected")
        summary = {
            "rows_total": report.rows_total, "rows_imported": imported, "rows_rejected": rows_rejected,
            "plots_created": plots_created, "visits_created": visits_created, "issues": _issues_out(issues),
        }
        batch.status, batch.summary = status, summary
        record_audit(
            db, principal=principal, action="field_import.commit", resource_type="import_batch",
            resource_id=batch.id, project_id=project_id, request_id=request_id, snapshot_hash=sha256,
            after={"status": status, "mode": mode, **{k: v for k, v in summary.items() if k != "issues"}},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"batch_id": batch.id, "status": status, "mode": mode, **summary}


def list_plots(db: Session, principal: Principal, project_id: str) -> list[dict[str, Any]]:
    authorize(db, project_id, principal, "field.read")
    visit_counts = (
        select(SurveyVisit.plot_id, func.count().label("n")).group_by(SurveyVisit.plot_id).subquery()
    )
    rows = db.execute(
        select(SurveyPlot, func.coalesce(visit_counts.c.n, 0))
        .outerjoin(visit_counts, visit_counts.c.plot_id == SurveyPlot.id)
        .where(SurveyPlot.project_id == project_id)
        .order_by(SurveyPlot.plot_code)
    ).all()
    return [
        {"plot_code": p.plot_code, "latitude": p.latitude, "longitude": p.longitude, "area_m2": p.area_m2,
         "area_basis": p.area_basis, "visit_count": int(n)}
        for p, n in rows
    ]


def list_visits(db: Session, principal: Principal, project_id: str, plot_code: str) -> list[dict[str, Any]]:
    authorize(db, project_id, principal, "field.read")
    plot = db.execute(
        select(SurveyPlot).where(SurveyPlot.project_id == project_id, SurveyPlot.plot_code == plot_code)
    ).scalar_one_or_none()
    if plot is None:
        raise DmrvError(404, "not_found", "Plot tidak ditemukan.")
    out = []
    for visit in db.execute(
        select(SurveyVisit).where(SurveyVisit.plot_id == plot.id).order_by(SurveyVisit.visit_date)
    ).scalars():
        biomass = list(db.execute(
            select(TreeMeasurement.biomass_kg).where(TreeMeasurement.visit_id == visit.id)
        ).scalars())
        density = plot_density(biomass, plot.area_m2, plot.area_basis)
        out.append({
            "visit_date": visit.visit_date.isoformat(),
            "date_precision": visit.date_precision,
            "tree_count": density.tree_count,
            "density": {
                "source_type": density.source_type,
                "biomass_dry_mg_per_ha": float(density.biomass_dry_mg_per_ha) if density.biomass_dry_mg_per_ha is not None else None,
                "carbon_mg_c_per_ha": float(density.carbon_mg_c_per_ha) if density.carbon_mg_c_per_ha is not None else None,
                "area_m2": float(density.area_m2) if density.area_m2 is not None else None,
                "area_basis": density.area_basis,
                "carbon_fraction": float(density.carbon_fraction),
                "trees_without_biomass": density.trees_without_biomass,
                "blockers": list(density.blockers),
            },
        })
    return out
