"""Transparent reviewer triage from project records; never a credit-loss decision."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.dmrv_advanced import DmrvFieldCapture
from app.db.models.dmrv_integrity import DmrvIntegrityRecord
from app.services.dmrv.authz import Principal, authorize


def reviewer_priority(db: Session, principal: Principal, project_id: str) -> dict:
    authorize(db, project_id, principal, "accounting.read")
    rows = db.execute(select(DmrvIntegrityRecord).where(DmrvIntegrityRecord.project_id == project_id)).scalars().all()
    risks = [row for row in rows if row.category == "risk" and row.status in {"open", "acknowledged"}]
    factors = []
    high = [row for row in risks if row.payload.get("severity") == "high"]
    medium = [row for row in risks if row.payload.get("severity") == "medium"]
    if high:
        factors.append({"key": "high_severity_open_risks", "count": len(high), "level": "urgent",
                        "detail": "Open high-severity risk records require human review."})
    if medium:
        factors.append({"key": "medium_severity_open_risks", "count": len(medium), "level": "elevated",
                        "detail": "Open medium-severity risk records remain unresolved."})

    provenance = [row for row in rows if row.category == "satellite_provenance"]
    if not provenance:
        factors.append({"key": "satellite_provenance_missing", "level": "elevated",
                        "detail": "No satellite source and quality record is linked to an analysis snapshot."})
    for row in provenance:
        aerosol = row.payload.get("aerosol_status")
        if aerosol in {"unknown", "not_corrected"}:
            factors.append({"key": "atmospheric_quality_unconfirmed", "level": "elevated",
                            "record_id": row.id,
                            "detail": "Aerosol correction is unconfirmed; inspect image QA and atmospheric conditions."})
        if row.payload.get("cloud_cover_pct") is None or row.payload.get("observation_count") is None:
            factors.append({"key": "satellite_coverage_incomplete", "level": "elevated",
                            "record_id": row.id,
                            "detail": "Cloud cover or observation count was not reported."})

    validation = sorted((row for row in rows if row.category == "validation"),
                        key=lambda row: row.updated_at, reverse=True)
    if not validation:
        factors.append({"key": "spatial_validation_missing", "level": "elevated",
                        "detail": "No uploaded spatial validation pairs are available for reviewer context."})
    else:
        metrics = (validation[0].result or {}).get("calibrated_oof")
        bootstrap = (validation[0].result or {}).get("calibrated_bootstrap") or {}
        intervals = (bootstrap.get("ci95") or {}) if bootstrap.get("status") == "conditional_fixed_predictions" else {}
        if metrics is None:
            factors.append({"key": "spatial_validation_incomplete", "level": "elevated",
                            "record_id": validation[0].id,
                            "detail": "The buffered spatial holdout did not produce complete out-of-fold calibration metrics."})
        elif not intervals:
            factors.append({"key": "validation_uncertainty_not_quantified", "level": "elevated",
                            "record_id": validation[0].id,
                            "detail": "Spatial holdout metrics exist, but block-bootstrap uncertainty intervals are unavailable."})
        else:
            rmse_ci = intervals.get("rmse")
            mae_ci = intervals.get("mae")
            factors.append({"key": "validation_uncertainty_available", "level": "information",
                            "record_id": validation[0].id,
                            "detail": f"Holdout RMSE={metrics.get('rmse')}; MAE={metrics.get('mae')}; fixed-prediction block-bootstrap 95% intervals: RMSE={rmse_ci}, MAE={mae_ci}. These describe the uploaded validation pairs, not carbon-stock uncertainty."})

    captures = db.execute(
        select(DmrvFieldCapture).where(DmrvFieldCapture.project_id == project_id)
    ).scalars().all()
    flagged_captures = [capture for capture in captures if capture.qa_flags]
    if not captures:
        factors.append({"key": "field_verification_missing", "level": "elevated",
                        "detail": "No geotagged field notes are available for reviewer comparison."})
    elif flagged_captures:
        factors.append({"key": "field_observations_need_review", "level": "elevated",
                        "count": len(flagged_captures),
                        "detail": f"{len(flagged_captures)} geotagged field note(s) have GPS/boundary QA flags."})

    accounting = sorted((row for row in rows if row.category == "accounting"),
                        key=lambda row: row.updated_at, reverse=True)
    if not accounting:
        factors.append({"key": "accounting_record_missing", "level": "elevated",
                        "detail": "No monitoring-period accounting record is available for reviewer context."})
    elif accounting[0].payload.get("uncertainty_deduction_pct") is None:
        factors.append({"key": "uncertainty_not_reported", "level": "elevated",
                        "record_id": accounting[0].id,
                        "detail": "The accounting record does not report an uncertainty deduction."})
    else:
        factors.append({"key": "uncertainty_reported", "level": "information",
                        "record_id": accounting[0].id,
                        "detail": f"Declared uncertainty deduction: {accounting[0].payload['uncertainty_deduction_pct']}%."})

    if high:
        priority, label = "urgent", "Review first"
    elif risks or any(factor["level"] == "elevated" for factor in factors):
        priority, label = "elevated", "Review soon"
    else:
        priority, label = "routine", "Routine review"
    return {
        "priority": priority,
        "label": label,
        "open_risks": len(risks),
        "factors": factors,
        "qualification": "Reviewer triage only. Satellite signals and missing metadata do not establish carbon loss, reversal, credit invalidity, or a change to issued units.",
    }
