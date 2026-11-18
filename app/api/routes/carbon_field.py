"""Authenticated multi-site field observations on the existing calibration datasets."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import require_permission
from app.db.models.carbon_calibration import (
    CarbonCalibrationDataset,
    CarbonCalibrationModel,
    CarbonCalibrationRun,
    CarbonFieldObservation,
)
from app.db.models.uploaded_model import UploadedModel
from app.db.session import get_db
from app.schemas.carbon_field import (
    FieldAssessmentRequest,
    FieldObservationBatch,
    FieldPrediction,
    FieldPredictionExtractionRequest,
)
from app.db.models.audit_log import AuditLog
from app.services.carbon_field_service import (
    CSV_COLUMNS,
    SOC_SOURCE,
    POOL_TARGETS,
    assess,
    field_inventory,
    normalize,
    read_csv,
)

router = APIRouter()


@router.get(
    "/datasets/{dataset_id}/field-summary", dependencies=[Depends(require_permission("calibration.read"))]
)
def field_summary(dataset_id: str, db: Session = Depends(get_db)):
    dataset = dataset_or_404(db, dataset_id)
    rows = db.query(CarbonFieldObservation).filter_by(dataset_pk=dataset.id).limit(20001).all()
    if len(rows) > 20000:
        raise HTTPException(
            422, "Pisahkan dataset lebih dari 20.000 pengukuran menurut wilayah/periode survei."
        )
    return field_inventory([row.to_dict() for row in rows])


def record_audit(db, admin, action, resource_type, resource_id, detail):
    # Data and audit must commit together; do not use the legacy best-effort logger.
    db.add(
        AuditLog(
            admin_user_id=admin.id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            detail=detail,
        )
    )


def dataset_or_404(db, dataset_id):
    dataset = db.query(CarbonCalibrationDataset).filter_by(dataset_id=dataset_id).first()
    if not dataset:
        raise HTTPException(404, "Dataset kalibrasi tidak ditemukan.")
    return dataset


def merge_prediction(existing, prediction):
    if prediction is None:
        return existing
    same = next((item for item in existing if item["source"] == prediction["source"]), None)
    if same is not None and same != prediction:
        raise HTTPException(
            409, "Sumber prediksi sudah memiliki nilai berbeda. Gunakan ID sumber/versi baru."
        )
    return existing if same else [*existing, prediction]


def _pool_model_candidates(db, pool: str, model_name: str | None):
    target_pool = POOL_TARGETS[pool]
    candidates = db.query(UploadedModel).filter_by(
        model_type="carbon", is_active=True, lifecycle_state="active"
    ).all()
    candidates = [
        model for model in candidates
        if (model.metadata_json or {}).get("format") == "gee_saved_ensemble"
        and (model.metadata_json or {}).get("method") == "saved_reference_ensemble"
        and (model.metadata_json or {}).get("target_pool") in {pool, target_pool, f"{pool}_carbon"}
    ]
    if model_name:
        candidates = [model for model in candidates if model.name == model_name]
    from app.services import model_lifecycle

    approved = []
    for model in candidates:
        try:
            evaluation = model_lifecycle.latest_evaluation(db, model)
            if not model_lifecycle.has_current_approval(db, model, evaluation):
                continue
            model_lifecycle.artifact_path(model)
        except HTTPException:
            continue
        validation = (model.metadata_json or {}).get("validation_metrics") or {}
        try:
            recorded_r2 = float(validation.get("r2", -1e9))
        except (TypeError, ValueError):
            recorded_r2 = -1e9
        if recorded_r2 <= 0:
            continue
        approved.append((recorded_r2, model))
    return sorted(approved, key=lambda item: (item[0], item[1].id), reverse=True)


@router.post("/datasets/{dataset_id}/field-predictions", status_code=202)
def queue_field_predictions(
    dataset_id: str,
    payload: FieldPredictionExtractionRequest,
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    dataset = dataset_or_404(db, dataset_id)
    if payload.analysis_year > datetime.now(UTC).year:
        raise HTTPException(422, "Tahun prediksi tidak boleh melebihi tahun berjalan.")
    selected = _pool_model_candidates(db, payload.pool, payload.model_name)
    if not selected:
        raise HTTPException(
            409,
            detail={
                "code": "approved_gee_model_unavailable",
                "message": "Belum ada model GEE untuk pool ini yang memiliki evaluasi tersimpan, persetujuan independen, dan artefak aktif.",
            },
        )
    _, model = selected[0]
    from app.services.model_serving_identity import current_identity

    rows_query = db.query(CarbonFieldObservation).filter_by(dataset_pk=dataset.id, pool=payload.pool)
    total = rows_query.count()
    rows = rows_query.order_by(CarbonFieldObservation.id).offset(payload.offset).limit(payload.limit).all()
    if not rows:
        raise HTTPException(404, "Tidak ada pengukuran untuk pool dan rentang batch ini.")

    metadata = model.metadata_json or {}
    run = CarbonCalibrationRun(
        id=str(uuid.uuid4()),
        dataset_pk=dataset.id,
        method="gee_field_prediction",
        target_pool=POOL_TARGETS[payload.pool],
        status="queued",
        progress=0,
        parameters={
            "pool": payload.pool,
            "analysis_year": payload.analysis_year,
            "offset": payload.offset,
            "batch_size": payload.limit,
            "total_observations": total,
            "observation_ids": [row.id for row in rows],
            "model_id": model.id,
            "model_name": model.name,
            "model_version": model.version,
            "artifact_sha256": model.artifact_sha256,
            "approval_digest": model.approval_digest,
            "target_pool": metadata.get("target_pool"),
            "serving_revision": current_identity(),
        },
        created_by=admin.id,
    )
    db.add(run)
    record_audit(
        db,
        admin,
        "calibration.field.predictions.queue",
        "carbon_calibration_run",
        run.id,
        {"pool": payload.pool, "analysis_year": payload.analysis_year, "model_id": model.id,
         "model_version": model.version, "offset": payload.offset, "batch_size": len(rows)},
    )
    db.commit()
    db.refresh(run)
    return {"run": run.to_dict(), "total_observations": total, "batch_count": len(rows)}


def import_records(db, dataset, records, admin):
    db.query(CarbonCalibrationDataset).filter_by(id=dataset.id).with_for_update().one()
    existing = db.query(CarbonFieldObservation).filter_by(dataset_pk=dataset.id).with_for_update().all()
    identities = {row.identity_key: row for row in existing}
    roles = {(row.pool, row.raw["plot_id"]): row.role for row in existing}
    created = skipped = 0
    for record in records:
        group = (record.pool, record.plot_id)
        if group in roles and roles[group] != record.role:
            raise HTTPException(
                422,
                f"Plot {record.plot_id}: peran kalibrasi/validasi harus tetap sama antar kunjungan dan kedalaman.",
            )
        roles[group] = record.role
        try:
            normalized = normalize(record)
        except ValueError as exc:
            raise HTTPException(422, f"Plot {record.plot_id}: {exc}") from exc
        row = identities.get(normalized["identity_key"])
        if row:
            if row.sha256 != normalized["sha256"]:
                raise HTTPException(
                    409,
                    f"Pengukuran {record.plot_id}/{record.survey_date}/{record.pool} sudah ada dan berbeda. Data tidak ditimpa.",
                )
            row.predictions = merge_prediction(row.predictions, normalized["prediction"])
            skipped += 1
        else:
            row = CarbonFieldObservation(
                id=str(uuid.uuid4()),
                dataset_pk=dataset.id,
                identity_key=normalized["identity_key"],
                sha256=normalized["sha256"],
                pool=record.pool,
                role=record.role,
                observed_mg_c_ha=normalized["observed_mg_c_ha"],
                raw=normalized["raw"],
                predictions=merge_prediction([], normalized["prediction"]),
                created_by=admin.id,
            )
            identities[row.identity_key] = row
            db.add(row)
            created += 1
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            409, "Pengukuran telah diimpor oleh permintaan lain. Muat ulang sebelum mencoba kembali."
        ) from exc
    # Audit commits the same transaction as these observations.
    record_audit(
        db,
        admin,
        "calibration.field.import",
        "carbon_calibration_dataset",
        dataset.id,
        {"created": created, "replayed": skipped},
    )
    db.commit()
    return {"created": created, "replayed": skipped, "unit": "Mg C/ha"}


@router.get("/field-template", dependencies=[Depends(require_permission("calibration.read"))])
def field_template():
    return Response(
        ",".join(CSV_COLUMNS) + "\r\n",
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="savegeo-field-agb-bgb-soc.csv"'},
    )


@router.get("/field-methods", dependencies=[Depends(require_permission("calibration.read"))])
def field_methods():
    return {
        "unit": "Mg C/ha",
        "soc_formula": "sum(C_g_kg * BD_g_cm3 * thickness_cm * 0.1 * (1 - coarse_volume_percent/100))",
        "soc_source": SOC_SOURCE,
        "bulk_density_basis": "fine_earth_per_fine_earth_volume",
        "roles": ["calibration", "validation"],
        "policy": "Plot independen; kunjungan berulang tidak dibagi antar data latih dan uji. BGB turunan diberi label eksplisit.",
    }


@router.get(
    "/datasets/{dataset_id}/observations", dependencies=[Depends(require_permission("calibration.read"))]
)
def observations(dataset_id: str, page: int = 1, pool: str | None = None, db: Session = Depends(get_db)):
    dataset = dataset_or_404(db, dataset_id)
    if page < 1:
        raise HTTPException(422, "Halaman minimal 1.")
    query = db.query(CarbonFieldObservation).filter_by(dataset_pk=dataset.id)
    if pool is not None:
        if pool not in POOL_TARGETS:
            raise HTTPException(422, "Pool tidak dikenal.")
        query = query.filter_by(pool=pool)
    total = query.count()
    rows = (
        query.order_by(CarbonFieldObservation.created_at, CarbonFieldObservation.id)
        .offset((page - 1) * 100)
        .limit(100)
        .all()
    )
    return {"rows": [row.to_dict() for row in rows], "total": total, "page": page, "page_size": 100}


@router.post("/datasets/{dataset_id}/observations")
def add_observations(
    dataset_id: str,
    payload: FieldObservationBatch,
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    return import_records(db, dataset_or_404(db, dataset_id), payload.records, admin)


@router.post("/datasets/{dataset_id}/observations/csv")
async def import_csv(
    dataset_id: str,
    preview: bool = True,
    file: UploadFile = File(...),
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    dataset = dataset_or_404(db, dataset_id)
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "Ukuran CSV maksimum 5 MB.")
    try:
        result = read_csv(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    report = {key: value for key, value in result.items() if key != "records"}
    if preview:
        return report
    if not result["valid"]:
        raise HTTPException(
            422, {"message": "Impor dibatalkan; perbaiki seluruh kesalahan CSV.", "errors": result["errors"]}
        )
    return {**report, **import_records(db, dataset, result["records"], admin)}


@router.post("/datasets/{dataset_id}/observations/{observation_id}/predictions")
def add_prediction(
    dataset_id: str,
    observation_id: str,
    payload: FieldPrediction,
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    dataset = dataset_or_404(db, dataset_id)
    row = (
        db.query(CarbonFieldObservation)
        .filter_by(id=observation_id, dataset_pk=dataset.id)
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(404, "Pengukuran tidak ditemukan.")
    row.predictions = merge_prediction(row.predictions, payload.model_dump(mode="json"))
    record_audit(
        db,
        admin,
        "calibration.field.prediction",
        "carbon_field_observation",
        row.id,
        {"source": payload.source},
    )
    db.commit()
    return row.to_dict()


@router.post("/datasets/{dataset_id}/field-assessments", status_code=201)
def assess_dataset(
    dataset_id: str,
    payload: FieldAssessmentRequest,
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    dataset = dataset_or_404(db, dataset_id)
    rows = (
        db.query(CarbonFieldObservation)
        .filter_by(dataset_pk=dataset.id, pool=payload.pool)
        .order_by(CarbonFieldObservation.id)
        .limit(20001)
        .all()
    )
    if len(rows) > 20000:
        raise HTTPException(
            422, "Pisahkan dataset lebih dari 20.000 pengukuran menjadi wilayah/periode survei."
        )
    try:
        result = assess([row.to_dict() for row in rows], payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result["provenance"] = {
        "dataset_id": dataset_id,
        "observation_ids": [row.id for row in rows],
        "snapshot_sha256": hashlib.sha256(
            json.dumps([row.to_dict() for row in rows], sort_keys=True).encode()
        ).hexdigest(),
    }
    run = CarbonCalibrationRun(
        id=str(uuid.uuid4()),
        dataset_pk=dataset.id,
        method="field_plot_calibration",
        target_pool=POOL_TARGETS[payload.pool],
        status="complete" if result["status"] == "computed" else "not_computed",
        progress=100,
        parameters=payload.model_dump(),
        metrics=result,
        created_by=admin.id,
        completed_at=datetime.now(UTC),
    )
    if result.get("artifact"):
        raw = [row.raw for row in rows if row.role == "calibration"]
        scope = {
            key: (dataset.manifest or {}).get(key)
            for key in ("site", "location", "ecosystem", "species", "survey_year")
        }
        scope.update(
            training_bbox=[
                min(r["longitude"] for r in raw),
                min(r["latitude"] for r in raw),
                max(r["longitude"] for r in raw),
                max(r["latitude"] for r in raw),
            ],
            prediction_source=payload.prediction_source,
            target_depth_cm=result["artifact"]["depth_cm"],
        )
        artifact = {
            **result["artifact"],
            "scope": scope,
            "source_dataset_ids": [dataset_id],
            "validation": result,
            "provenance": result["provenance"],
        }
        content = json.dumps(artifact, sort_keys=True, indent=2)
        root = get_settings().upload_path / "carbon-calibration" / dataset_id / "models"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{run.id}.json"
        path.write_text(content, encoding="utf-8")
        run.artifact_path, run.artifact_sha256 = str(path), hashlib.sha256(content.encode()).hexdigest()
        db.add(run)
        db.flush()
        db.add(
            CarbonCalibrationModel(
                model_id=f"field_{run.id}",
                dataset_pk=dataset.id,
                run_id=run.id,
                model_name=f"{dataset.name} · {payload.pool.upper()} · Field calibration",
                version="1.0.0",
                target_pool=run.target_pool,
                target_unit="Mg C/ha",
                status="experimental",
                is_active=False,
                artifact_path=str(path),
                artifact_sha256=run.artifact_sha256,
                metadata_json={
                    **scope,
                    "validation_method": "leave_one_plot_out_and_separate_validation",
                    "independent_sample_count": result["calibration_plots"],
                    "field_schema": "v1",
                },
            )
        )
    else:
        db.add(run)
    record_audit(
        db,
        admin,
        "calibration.field.assess",
        "carbon_calibration_run",
        run.id,
        {"pool": payload.pool, "prediction_source": payload.prediction_source},
    )
    db.commit()
    return run.to_dict()


@router.get(
    "/datasets/{dataset_id}/field-assessments", dependencies=[Depends(require_permission("calibration.read"))]
)
def assessment_history(dataset_id: str, db: Session = Depends(get_db)):
    dataset = dataset_or_404(db, dataset_id)
    runs = (
        db.query(
            CarbonCalibrationRun.id,
            CarbonCalibrationRun.target_pool,
            CarbonCalibrationRun.status,
            CarbonCalibrationRun.created_at,
        )
        .filter_by(dataset_pk=dataset.id, method="field_plot_calibration")
        .order_by(CarbonCalibrationRun.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        "runs": [
            {
                "id": run.id,
                "target_pool": run.target_pool,
                "status": run.status,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
            for run in runs
        ]
    }
