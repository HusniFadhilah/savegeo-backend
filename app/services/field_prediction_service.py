"""Create immutable, plot-level predictions from an approved saved GEE model."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime

import ee
from sqlalchemy.orm import Session

from app.db.models.carbon_calibration import (
    CarbonCalibrationDataset,
    CarbonCalibrationRun,
    CarbonFieldObservation,
    FieldPlot,
)
from app.db.models.uploaded_model import UploadedModel
from app.services import model_lifecycle
from app.services.gee_common import AnalysisError


def _model_for_run(db: Session, parameters: dict) -> tuple[UploadedModel, dict]:
    model = db.query(UploadedModel).filter_by(
        id=parameters.get("model_id"), model_type="carbon"
    ).first()
    if (
        model is None
        or model.name != parameters.get("model_name")
        or model.version != parameters.get("model_version")
        or model.artifact_sha256 != parameters.get("artifact_sha256")
        or model.approval_digest != parameters.get("approval_digest")
        or model.lifecycle_state != "active"
        or not model.is_active
    ):
        raise AnalysisError("Versi model berubah atau tidak lagi aktif. Buat ekstraksi baru dengan model yang disetujui.", 409)
    metadata = model.metadata_json or {}
    if (
        metadata.get("format") != "gee_saved_ensemble"
        or metadata.get("method") != "saved_reference_ensemble"
        or metadata.get("target_pool") != parameters.get("target_pool")
    ):
        raise AnalysisError("Model GEE tidak kompatibel dengan pool pengukuran ini.", 409)
    from app.services.model_serving_identity import matches

    if not matches(parameters.get("serving_revision")):
        raise AnalysisError(
            "Versi serving GEE berubah setelah ekstraksi dijadwalkan. Job dihentikan; mulai ekstraksi baru.",
            409,
        )
    evaluation = model_lifecycle.latest_evaluation(db, model)
    if not model_lifecycle.has_current_approval(db, model, evaluation):
        raise AnalysisError("Bukti evaluasi dan persetujuan model tidak lagi cocok.", 409)
    model_lifecycle.artifact_path(model)
    return model, metadata


def _plot_feature(row: CarbonFieldObservation, plot: FieldPlot | None) -> ee.Feature:
    geometry = (plot.geometry_normalized if plot else None) or None
    if isinstance(geometry, dict) and geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") in {"Polygon", "MultiPolygon"}:
        ee_geometry = ee.Geometry(geometry, proj="EPSG:4326", geodesic=False)
        support = "plot_geometry"
    else:
        raw = row.raw or {}
        area_m2 = float(raw.get("plot_area_m2") or 0)
        longitude, latitude = float(raw["longitude"]), float(raw["latitude"])
        radius = math.sqrt(area_m2 / math.pi)
        if not math.isfinite(radius) or radius <= 0 or radius > 1000:
            raise ValueError(f"Plot {raw.get('plot_id')} membutuhkan geometri; luas ekuivalen lingkaran harus 0–1.000 m radius.")
        ee_geometry = ee.Geometry.Point([longitude, latitude]).buffer(radius, maxError=1)
        support = "equal_area_circle_proxy"
    return ee.Feature(ee_geometry, {"observation_id": row.id, "support": support})


def run_field_prediction(db: Session, run_id: str, worker_id: str) -> None:
    run = db.get(CarbonCalibrationRun, run_id)
    if run is None or run.status != "running" or run.worker_id != worker_id:
        raise AnalysisError("Lease pekerjaan ekstraksi tidak lagi dimiliki worker ini.", 409)
    parameters = dict(run.parameters or {})
    dataset = db.get(CarbonCalibrationDataset, run.dataset_pk)
    if dataset is None:
        raise AnalysisError("Dataset lapangan tidak ditemukan.", 404)
    model, metadata = _model_for_run(db, parameters)
    observation_ids = parameters.get("observation_ids")
    if not isinstance(observation_ids, list) or not observation_ids or len(observation_ids) > 1000:
        raise AnalysisError("Daftar plot untuk ekstraksi tidak valid.", 422)
    rows = db.query(CarbonFieldObservation).filter(
        CarbonFieldObservation.dataset_pk == dataset.id,
        CarbonFieldObservation.id.in_(observation_ids),
        CarbonFieldObservation.pool == parameters.get("pool"),
    ).order_by(CarbonFieldObservation.id).all()
    if len(rows) != len(set(observation_ids)):
        raise AnalysisError("Sebagian pengukuran berubah atau tidak tersedia; muat ulang dataset.", 409)

    plots = db.query(FieldPlot).filter_by(dataset_pk=dataset.id).all()
    plots_by_id = {plot.plot_id: plot for plot in plots}
    features = [_plot_feature(row, plots_by_id.get(str((row.raw or {}).get("plot_id")))) for row in rows]
    collection = ee.FeatureCollection(features)
    roi = collection.geometry().bounds(maxError=100)
    analysis_year = int(parameters["analysis_year"])
    model_spec = {"name": model.name, "version": model.version, "algorithm": model.algorithm,
                  "description": model.description, "metadata_json": metadata}
    reference_year = int(metadata.get("reference_year") or metadata.get("covariate_year") or 2010)
    prediction_image, model_details = __import__(
        "app.services.carbon_saved_models", fromlist=["saved_model_image"]
    ).saved_model_image(roi, model_spec, {"year": reference_year, "selection_year": reference_year}, analysis_year=analysis_year)
    map_scale = max(1, int(model_details.get("map_resolution_m") or 30))
    sampled = prediction_image.rename("prediction").reduceRegions(
        collection=collection, reducer=ee.Reducer.mean(), scale=map_scale, tileScale=4,
    ).getInfo()
    observed = {
        item["properties"].get("observation_id"): item["properties"]
        for item in sampled.get("features", []) if isinstance(item.get("properties"), dict)
    }
    source = f"gee:{model.name[:120]}@{str(model.version or 'unknown')[:40]}:{analysis_year}:{model.artifact_sha256[:8]}"
    saved = reused = 0
    without_pixels: list[str] = []
    snapshot_rows: list[dict] = []
    pending_updates: list[tuple[CarbonFieldObservation, dict]] = []
    for row in rows:
        values = observed.get(row.id) or {}
        prediction = values.get("prediction_mean")
        if prediction is None:
            prediction = values.get("mean", values.get("agb_mean"))
        if prediction is None or not math.isfinite(float(prediction)):
            without_pixels.append(row.id)
            continue
        prediction_value = max(0.0, float(prediction))
        item = {
            "source": source, "year": analysis_year, "value_mg_c_ha": prediction_value,
            "scale_m": float(map_scale), "support": "plot_mean",
            "depth_top_cm": metadata.get("depth_cm", [None, None])[0]
            if parameters.get("pool") == "soc" and isinstance(metadata.get("depth_cm"), list) else None,
            "depth_bottom_cm": metadata.get("depth_cm", [None, None])[1]
            if parameters.get("pool") == "soc" and isinstance(metadata.get("depth_cm"), list) else None,
            "sampling_geometry": str(values.get("support") or "plot_mean"),
        }
        existing = list(row.predictions or [])
        prior = next((entry for entry in existing if entry.get("source") == source), None)
        if prior is not None:
            if (abs(float(prior.get("value_mg_c_ha", -1)) - prediction_value) > 1e-8
                    or prior.get("year") != analysis_year or prior.get("scale_m") != float(map_scale)):
                raise AnalysisError("Prediksi GEE tersimpan berbeda untuk identitas model/tahun ini; data lama dipertahankan.", 409)
            item = prior
            reused += 1
        else:
            pending_updates.append((row, item))
            saved += 1
        snapshot_rows.append({"observation_id": row.id, **item})

    snapshot_sha256 = hashlib.sha256(
        json.dumps(snapshot_rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    # Fence the final data writes after the long Earth Engine request. A worker
    # whose lease was reaped cannot publish predictions after another attempt.
    locked_run = db.query(CarbonCalibrationRun).filter_by(id=run_id).with_for_update().one_or_none()
    now = datetime.now(UTC)
    lease_until = locked_run.lease_expires_at if locked_run else None
    if lease_until and lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    if (locked_run is None or locked_run.status != "running" or locked_run.worker_id != worker_id
            or lease_until is None or lease_until <= now):
        db.rollback()
        raise AnalysisError("Lease pekerjaan kedaluwarsa sebelum hasil dapat disimpan.", 409)
    for row, item in pending_updates:
        row.predictions = [*(row.predictions or []), item]
    parameters.update(prediction_source=source, prediction_snapshot_sha256=snapshot_sha256)
    locked_run.parameters = parameters
    locked_run.metrics = {
        "status": "computed" if saved else "not_computed", "pool": parameters.get("pool"),
        "analysis_year": analysis_year, "prediction_source": source,
        "model": {"id": model.id, "name": model.name, "version": model.version,
                  "artifact_sha256": model.artifact_sha256, "approval_digest": model.approval_digest},
        "model_validation": (metadata.get("validation_metrics") or {}), "sampled": len(rows),
        "predictions_saved": saved, "predictions_reused": reused,
        "without_valid_pixels": len(without_pixels), "without_valid_pixel_observation_ids": without_pixels,
        "prediction_snapshot_sha256": snapshot_sha256,
        "support_note": "Geometri plot tersimpan digunakan bila tersedia; jika tidak, area plot didekati lingkaran dengan luas setara dari titik pusat.",
    }
    locked_run.progress, locked_run.status = 100, "complete" if saved else "not_computed"
    locked_run.completed_at = now
    locked_run.worker_id = None
    locked_run.lease_expires_at = None
    db.commit()
