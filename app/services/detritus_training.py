"""Train once, retain frozen trees, and reuse the best spatially evaluated model."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import ee
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold
from sqlalchemy import text

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel, UploadedModelEvaluation
from app.services.carbon_saved_models import FEATURES, saved_reference_models, predictors, tree_string
from app.services.detritus_reference import KEYS, SOURCE, reference_image
from app.services.gee_common import AnalysisError

BBOX = [94.0, -12.0, 142.0, 7.0]


def _spatial_groups(frame):
    return np.floor(frame.longitude.to_numpy() / 0.5).astype(int) * 10000 + np.floor(
        frame.latitude.to_numpy() / 0.5
    ).astype(int)


def _spatial_cv_metrics(frame, groups):
    x, y = frame[FEATURES].to_numpy(), frame.agb.to_numpy()
    if len(np.unique(groups)) < 3:
        raise AnalysisError("Pelatihan membutuhkan minimal tiga blok spasial independen.", 422)
    predictions = np.zeros(len(y))
    for train, valid in GroupKFold(n_splits=3).split(x, y, groups):
        estimator = RandomForestRegressor(
            n_estimators=60, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=2
        )
        estimator.fit(x[train], y[train])
        predictions[valid] = estimator.predict(x[valid])
    metrics = {
        "r2": float(r2_score(y, predictions)),
        "rmse": float(np.sqrt(mean_squared_error(y, predictions))),
        "mae": float(mean_absolute_error(y, predictions)),
        "bias": float(np.mean(predictions - y)),
    }
    return metrics


def _has_current_proxy_evaluation(db, row):
    from app.services import model_evaluation_storage

    evaluation = db.query(UploadedModelEvaluation).filter_by(
        model_id=row.id, artifact_sha256=row.artifact_sha256
    ).order_by(UploadedModelEvaluation.created_at.desc(), UploadedModelEvaluation.id.desc()).first()
    return bool(evaluation and model_evaluation_storage.verify(
        evaluation.id, evaluation.dataset_sha256, evaluation.dataset_storage_path
    ))


def _store_spatial_evaluation(db, row, frame, groups, metrics, pool):
    """Persist the exact encrypted proxy samples and their spatial-CV result."""
    import hashlib

    from app.services import model_evaluation_storage

    if _has_current_proxy_evaluation(db, row):
        return
    review_frame = frame[FEATURES].copy()
    review_frame["agb"] = frame.agb.to_numpy()
    review_frame["spatial_group"] = groups.astype(str)
    raw = review_frame.to_csv(index=False, float_format="%.10g").encode("utf-8")
    dataset_sha256 = hashlib.sha256(raw).hexdigest()
    record = UploadedModelEvaluation(
        model_id=row.id,
        artifact_sha256=row.artifact_sha256,
        dataset_name=f"Indonesia {pool} proxy raster · spatial CV",
        dataset_sha256=dataset_sha256,
        dataset_source=SOURCE,
        manifest={
            "evaluation_method": "group_kfold_out_of_fold",
            "fold_count": 3,
            "group_column": "spatial_group",
            "independent_group_count": int(len(np.unique(groups))),
            "sample_count": int(len(frame)),
            "training_mode": "derived_reference_raster",
            "source_data_kind": "proxy_not_field_measurement",
            "source_dataset_key": row.metadata_json.get("target_dataset_key"),
            "interpretation": "Evaluasi silang spasial atas label raster proxy; bukan validasi lapangan dan bukan bukti akurasi lapangan.",
        },
        metrics={
            "metric_type": "regression",
            "sample_count": int(len(frame)),
            "independent_group_count": int(len(np.unique(groups))),
            "r2": metrics["r2"],
            "rmse": metrics["rmse"],
            "mae": metrics["mae"],
            "bias_prediction_minus_observed": metrics["bias"],
        },
    )
    db.add(record)
    db.flush()
    relative_path = model_evaluation_storage.store(record.id, dataset_sha256, raw)
    record.dataset_storage_path = relative_path
    row.lifecycle_state = "evaluated"
    row.metadata_json = {**(row.metadata_json or {}), "evaluation_storage": "encrypted", "evaluation_dataset_sha256": dataset_sha256}
    db.commit()


def _evaluate_existing_proxy_model(db, row, root, pool):
    """Upgrade a legacy quarantined ensemble once, only after recomputing its CV metrics."""
    if _has_current_proxy_evaluation(db, row):
        return
    meta = row.metadata_json or {}
    if meta.get("method") != "saved_reference_ensemble" or meta.get("source_data_kind") != "proxy_not_field_measurement":
        return
    artifact = (root / str(meta.get("frozen_ensemble_file") or "")).resolve()
    if not artifact.is_relative_to(root.resolve()) or not artifact.is_file():
        return
    import hashlib

    if hashlib.sha256(artifact.read_bytes()).hexdigest() != row.artifact_sha256:
        return
    samples_name = str(meta.get("frozen_ensemble_file") or "")
    samples_path = root / f"{samples_name.removesuffix('.ensemble.json')}.samples.json"
    if not samples_path.is_file():
        return
    frame = pd.read_json(samples_path).replace([np.inf, -np.inf], np.nan)
    if frame.empty or any(name not in frame for name in FEATURES + ["agb", "longitude", "latitude"]):
        return
    frame = frame.dropna(subset=FEATURES + ["agb", "longitude", "latitude"])
    groups = _spatial_groups(frame)
    metrics = _spatial_cv_metrics(frame, groups)
    recorded = meta.get("validation_metrics") or {}
    if metrics["r2"] <= 0 or any(
        key not in recorded or abs(metrics[key] - float(recorded[key])) > 1e-6
        for key in ("r2", "rmse", "mae", "bias")
    ):
        return
    _store_spatial_evaluation(db, row, frame, groups, metrics, pool)


def ensure_model(db, pool):
    key = KEYS[pool]
    name = f"reference_{key.lower()}_2010_random_forest_indonesia_v1"
    existing = saved_reference_models(key, db=db)
    if existing:
        return max(
            existing, key=lambda item: item["metadata_json"].get("validation_metrics", {}).get("r2", -1e9)
        )
    # The transaction-level lock prevents two worker processes fitting the same pool.
    if db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int(hashlib.sha256(key.encode()).hexdigest()[:15], 16)},
        )
    existing = saved_reference_models(key, db=db)
    if existing:
        db.commit()
        return existing[0]
    # A quarantined model must not be retrained for every analysis. Recompute
    # its spatial CV from the retained sample file once, then leave the model
    # waiting for independent review before it can serve again.
    prior = db.query(UploadedModel).filter_by(name=name, model_type="carbon").first()
    if prior and (prior.metadata_json or {}).get("method") == "saved_reference_ensemble":
        root = get_settings().model_path.resolve()
        if (prior.metadata_json or {}).get("format") != "gee_saved_ensemble":
            prior.metadata_json = {**(prior.metadata_json or {}), "format": "gee_saved_ensemble"}
            db.commit()
        if prior.lifecycle_state != "active":
            try:
                _evaluate_existing_proxy_model(db, prior, root, pool)
            except Exception:  # noqa: BLE001 - unavailable encryption key leaves model quarantined
                db.rollback()
                prior = db.query(UploadedModel).filter_by(id=prior.id).first()
                if prior:
                    prior.lifecycle_state = "experimental"
                    prior.approved_by = prior.approved_at = prior.approval_digest = None
                    prior.metadata_json = {**(prior.metadata_json or {}), "evaluation_blocker": "encrypted_evaluation_storage_unavailable"}
                    db.commit()
        return prior.to_dict_public()
    roi = ee.Geometry.Rectangle(BBOX, proj="EPSG:4326", geodesic=False)
    country = ee.FeatureCollection("FAO/GAUL/2015/level0").filter(ee.Filter.eq("ADM0_NAME", "Indonesia"))
    image = predictors(roi, 2010, FEATURES)
    label = reference_image(pool, ee)
    stack = image.addBands(label).addBands(ee.Image.pixelLonLat()).clipToCollection(country)
    sampled = stack.sample(
        region=roi, scale=1000, numPixels=6000, seed=42, tileScale=4, geometries=False
    ).getInfo()
    frame = pd.DataFrame([f["properties"] for f in sampled["features"]]).replace([np.inf, -np.inf], np.nan)
    if frame.empty or any(key not in frame for key in FEATURES + ["agb", "longitude", "latitude"]):
        raise AnalysisError("Label/kovariat pelatihan serasah atau kayu mati tidak tersedia.", 422)
    frame = frame.dropna(subset=FEATURES + ["agb", "longitude", "latitude"])
    if len(frame) < 200 or frame.agb.std() < 1e-8:
        raise AnalysisError(
            "Minimal 200 label referensi bervariasi diperlukan untuk model serasah/kayu mati.", 422
        )
    x, y = frame[FEATURES].to_numpy(), frame.agb.to_numpy()
    groups = _spatial_groups(frame)
    metrics = _spatial_cv_metrics(frame, groups)
    if metrics["r2"] <= 0:
        raise AnalysisError(
            "Model serasah/kayu mati belum mengungguli baseline pada validasi spasial; model tidak diterbitkan.",
            422,
        )
    estimator = RandomForestRegressor(
        n_estimators=60, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=2
    )
    estimator.fit(x, y)
    artifact = {
        "algorithm": "random_forest",
        "trees": [tree_string(tree, FEATURES) for tree in estimator.estimators_],
    }
    # Confirm the frozen inference is numerically equivalent before registration.
    probes = [
        ee.Feature(
            None,
            {**{name: float(np.float32(value)) for name, value in zip(FEATURES, values)}, "sample_id": i},
        )
        for i, values in enumerate(x[:8])
    ]
    checked = (
        ee.FeatureCollection(probes)
        .classify(
            ee.Classifier.decisionTreeEnsemble(artifact["trees"]).setOutputMode("REGRESSION"), "prediction"
        )
        .getInfo()
    )
    actual = [
        item["properties"]["prediction"]
        for item in sorted(checked["features"], key=lambda item: item["properties"]["sample_id"])
    ]
    parity = float(np.max(np.abs(np.array(actual) - estimator.predict(x[:8]))))
    if parity > 1e-4:
        raise AnalysisError("Artefak model tidak lolos pemeriksaan kesetaraan inferensi.", 500)
    root = get_settings().model_path
    path = root / f"{name}.ensemble.json"
    content = json.dumps(artifact, allow_nan=False)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    metadata = {
        "method": "saved_reference_ensemble",
        "format": "gee_saved_ensemble",
        "provider": "builtin_gee",
        "builtin": True,
        "gee_deployable": True,
        "frozen_ensemble_file": path.name,
        "artifact_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "compatible_reference_datasets": [key],
        "target_dataset_key": key,
        "target_pool": f"{pool}_carbon",
        "target_unit": "Mg C/ha",
        "feature_names": FEATURES,
        "reference_year": 2010,
        "covariate_year": 2010,
        "training_bbox": BBOX,
        "training_domain": "indonesia",
        "training_country_mask": "FAO/GAUL/2015/level0:Indonesia",
        "time_series_supported": False,
        "preliminary": True,
        "n_samples": len(frame),
        "validation_samples": len(frame),
        "validation_metrics": metrics,
        "validation_type": "spatial_proxy_reference_holdout",
        "sample_scale_m": 1000,
        "spatial_block_degrees": 0.5,
        "trained_at": datetime.now(UTC).isoformat(),
        "frozen_gee_parity_max_error": parity,
        "training_mode": "derived_reference_raster",
        "source_url": SOURCE,
        "source_data_kind": "proxy_not_field_measurement",
        "description": "Frozen RF trained on tropical forest proxy labels derived from NASA/ORNL 2010 and UNFCCC factors. Annual Landsat predictors estimate the selected year. Spatial reference agreement is not field accuracy; mangroves excluded.",
    }
    row = db.query(UploadedModel).filter_by(name=name).first()
    if row is None:
        row = UploadedModel(name=name, model_type="carbon")
        db.add(row)
    row.display_name = f"RF · {pool.title()} · Indonesia (trained)"
    row.algorithm, row.version, row.is_active, row.is_default = "random_forest", "1.0", False, False
    row.lifecycle_state = "experimental"
    row.approved_by = row.approved_at = row.approval_digest = None
    row.filename, row.filepath, row.file_size_kb = path.name, str(path.resolve()), path.stat().st_size / 1024
    row.artifact_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    row.description, row.feature_names, row.metadata_json = metadata["description"], FEATURES, metadata
    row.metrics = {
        "cv_metrics": {"r2_mean": metrics["r2"], "rmse_mean": metrics["rmse"], "mae_mean": metrics["mae"]},
        "n_samples": len(frame),
    }
    db.commit()
    try:
        _store_spatial_evaluation(db, row, frame, groups, metrics, pool)
    except Exception:  # noqa: BLE001 - encryption/storage failure must leave the model inactive
        db.rollback()
        row = db.query(UploadedModel).filter_by(id=row.id).first()
        if row:
            row.lifecycle_state = "experimental"
            row.approved_by = row.approved_at = row.approval_digest = None
            row.metadata_json = {**(row.metadata_json or {}), "evaluation_blocker": "encrypted_evaluation_storage_unavailable"}
            db.commit()
    return row.to_dict_public()
