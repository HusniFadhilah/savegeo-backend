"""dMRV field data: import preview/commit and read models.

An import never overwrites: a tree (plot + visit date + tree code) that already
exists rejects the incoming row with ``tree_exists``. Trees with new codes are
appended to an existing visit, so corrected rows can be re-imported after a
partial import; each tree records the batch that brought it. Atomic mode saves
nothing but the batch record when any row is invalid; partial mode saves the
valid rows and reports the rest. The audit event is written in the same
transaction as the data.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any, Literal

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.dmrv_audit import DmrvIdempotencyKey
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
_IN_CHUNK = 5000  # keeps IN (...) lists far below database bind-parameter limits
LIVE_STATUSES = frozenset({"live", "recruit"})  # dead and missing trees are not living biomass


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
    dates = {tree.visit_date for tree in report.trees}
    existing: set[tuple[Any, ...]] = set()
    for chunk in _chunks(sorted({tree.plot_code for tree in report.trees})):
        existing.update(
            tuple(row) for row in db.execute(
                select(SurveyPlot.plot_code, SurveyVisit.visit_date, TreeMeasurement.tree_code)
                .join(SurveyVisit, SurveyVisit.plot_id == SurveyPlot.id)
                .join(TreeMeasurement, TreeMeasurement.visit_id == SurveyVisit.id)
                .where(
                    SurveyPlot.project_id == project_id,
                    SurveyPlot.plot_code.in_(chunk),
                    SurveyVisit.visit_date.in_(dates),
                )
            ).all()
        )
    for tree in report.trees:
        if (tree.plot_code, tree.visit_date, tree.tree_code) in existing:
            rejected.add(tree.row)
            extra.append(RowIssue(
                tree.row, "error", "tree_exists",
                f"Pohon {tree.tree_code} pada plot {tree.plot_code} tanggal {tree.visit_date} sudah tercatat; impor tidak menimpa data lama.",
            ))
    return headers, rows, mapping, report, extra, rejected


def _chunks(values: list[str], size: int = _IN_CHUNK) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _issues_out(issues: list[RowIssue]) -> list[dict[str, Any]]:
    """Errors first (a real error must never be cut off by hundreds of warnings), then by row."""
    ordered = sorted(issues, key=lambda i: (i.level != "error", i.row, i.code))
    return [asdict(issue) for issue in ordered[:MAX_ISSUES]]


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


def _replay(db: Session, principal: Principal, storage_key: str, request_hash: str) -> dict[str, Any] | None:
    row = db.execute(
        select(DmrvIdempotencyKey).where(
            DmrvIdempotencyKey.principal_key == principal.key, DmrvIdempotencyKey.key == storage_key
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise DmrvError(422, "idempotency_key_reused", "Idempotency-Key sudah dipakai dengan isi permintaan berbeda.")
    return {**row.response_body, "replayed": True}


def commit_import(
    db: Session, principal: Principal, project_id: str, filename: str, data: bytes,
    mapping: dict[str, str] | None, options: ImportOptions, mode: Literal["atomic", "partial"],
    request_id: str | None, idempotency_key: str | None = None,
) -> dict[str, Any]:
    authorize(db, project_id, principal, "field.write")
    storage_key = request_hash = None
    if idempotency_key:
        storage_key = f"field-import:{project_id}:{idempotency_key}"
        request_hash = hashlib.sha256(json.dumps(
            {"file": hashlib.sha256(data).hexdigest(), "name": filename, "mapping": mapping,
             "options": asdict(options), "mode": mode}, sort_keys=True).encode()).hexdigest()
        replayed = _replay(db, principal, storage_key, request_hash)
        if replayed is not None:
            return replayed
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
                for chunk in _chunks(sorted({tree.plot_code for tree in accepted}))
                for plot in db.execute(
                    select(SurveyPlot).where(SurveyPlot.project_id == project_id, SurveyPlot.plot_code.in_(chunk))
                ).scalars()
            }
            visits: dict[tuple[str, Any], SurveyVisit] = {}
            tree_rows: list[dict[str, Any]] = []
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
                tree_rows.append({
                    "visit_id": visit.id, "tree_code": tree.tree_code, "species": tree.species,
                    "circumference_cm": tree.circumference_cm, "dbh_cm": tree.dbh_cm, "height_m": tree.height_m,
                    "biomass_kg": tree.biomass_kg, "status": tree.status,
                    "biomass_source": "reported" if tree.biomass_kg is not None else None, "qa_flags": list(tree.flags),
                    "batch_id": batch.id,
                })
                imported += 1
            if tree_rows:
                db.flush()  # plots and visits first (foreign keys), then one bulk insert for the trees
                db.execute(insert(TreeMeasurement), tree_rows)
        rows_rejected = len(rejected)
        status = "committed" if not rejected else ("partial" if imported else "rejected")
        summary = {
            "rows_total": report.rows_total, "rows_imported": imported, "rows_rejected": rows_rejected,
            "plots_created": plots_created, "visits_created": visits_created, "issues": _issues_out(issues),
        }
        batch.status, batch.summary = status, summary
        result = {"batch_id": batch.id, "status": status, "mode": mode, **summary}
        if storage_key:
            db.add(DmrvIdempotencyKey(
                principal_key=principal.key, key=storage_key, request_hash=request_hash,
                response_status=201, response_body=result,
            ))
        record_audit(
            db, principal=principal, action="field_import.commit", resource_type="import_batch",
            resource_id=batch.id, project_id=project_id, request_id=request_id, snapshot_hash=sha256,
            after={"status": status, "mode": mode, **{k: v for k, v in summary.items() if k != "issues"}},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = _replay(db, principal, storage_key, request_hash) if storage_key else None
        if concurrent is None:
            raise DmrvError(
                409, "conflict", "Data bentrok dengan impor lain yang berjalan bersamaan; periksa hasilnya lalu coba lagi.",
                retryable=True,
            ) from None
        return concurrent
    except Exception:
        db.rollback()
        raise
    return result


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
    return plot_visits(db, plot)


def plot_visits(db: Session, plot: SurveyPlot) -> list[dict[str, Any]]:
    """Visits of one plot with their living-biomass density (no authorization: callers check it)."""
    trees_by_visit: dict[str, list[tuple[str, float | None]]] = {}
    for visit_id, status, biomass in db.execute(  # one query for every visit of the plot
        select(TreeMeasurement.visit_id, TreeMeasurement.status, TreeMeasurement.biomass_kg)
        .join(SurveyVisit, SurveyVisit.id == TreeMeasurement.visit_id)
        .where(SurveyVisit.plot_id == plot.id)
    ).all():
        trees_by_visit.setdefault(visit_id, []).append((status, biomass))
    out = []
    for visit in db.execute(
        select(SurveyVisit).where(SurveyVisit.plot_id == plot.id).order_by(SurveyVisit.visit_date)
    ).scalars():
        trees = trees_by_visit.get(visit.id, [])
        living = [biomass for status, biomass in trees if status in LIVE_STATUSES]
        density = plot_density(living, plot.area_m2, plot.area_basis)
        out.append({
            "visit_date": visit.visit_date.isoformat(),
            "date_precision": visit.date_precision,
            "tree_count": len(trees),
            "density": {
                "trees_excluded": len(trees) - len(living),  # dead / missing: not living biomass
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
