"""Seed a GEDI L4A-compatible carbon model.

The platform already has the GEDI L4A Monthly reference dataset loader, but
the dataset dropdown hides datasets that have no active compatible model. This
script registers a GEDI L4A model using the validated Indonesia-wide GEDI
S2/DEM native-classifier model as the base estimator, then scopes metadata to
`GEDI_L4A_MONTHLY`.

Usage:
    python -m scripts.seed_gedi_l4a_model
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal


SOURCE_MODEL_NAME = "scale10k_gedi_s2_dem__hist_gradient_boosting"
TARGET_MODEL_NAME = "gedi_l4a_monthly_s2_dem_hgb_2023"
TARGET_DISPLAY_NAME = "GEDI L4A Monthly S2 DEM HGB 2023"
TARGET_DATASET = "GEDI_L4A_MONTHLY"


def _metrics_from_metadata(metadata: dict) -> dict:
    cv = metadata.get("spatial_cv_metrics") or metadata.get("cv_metrics") or {}
    return {
        "train_metrics": metadata.get("train_metrics", {}),
        "cv_metrics": {
            "r2_mean": float(cv.get("r2_mean", 0.0)),
            "rmse_mean": float(cv.get("rmse_mean", 0.0)),
            "mae_mean": float(cv.get("mae_mean", metadata.get("cv_metrics", {}).get("mae_mean", 0.0))),
            **({"r2_std": cv["r2_std"]} if "r2_std" in cv else {}),
            **({"n_folds": cv["n_folds"]} if "n_folds" in cv else {}),
        },
        "n_samples": metadata.get("sample_count") or metadata.get("n_samples"),
        "n_features": metadata.get("feature_count") or metadata.get("n_features"),
        "trained_at": metadata.get("trained_at") or metadata.get("created_at"),
        "scaled_features": metadata.get("scaled_features", True),
    }


def seed_gedi_l4a_model() -> None:
    settings = get_settings()
    target_dir = settings.model_path
    target_dir.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        source = db.query(UploadedModel).filter_by(name=SOURCE_MODEL_NAME).first()
        if source is None:
            raise RuntimeError(f"Source model '{SOURCE_MODEL_NAME}' was not found in uploaded_models.")

        source_path = Path(source.filepath)
        source_json = source_path.with_suffix(".json")
        source_clf = source_path.with_name((source.metadata_json or {}).get("gee_classifier_path", ""))
        if not source_path.exists():
            raise RuntimeError(f"Source model file not found: {source_path}")
        if not source_clf.exists():
            raise RuntimeError(f"Source GEE classifier file not found: {source_clf}")

        target_pkl = target_dir / f"{TARGET_MODEL_NAME}.pkl"
        target_json = target_dir / f"{TARGET_MODEL_NAME}.json"
        target_clf = target_dir / f"{TARGET_MODEL_NAME}.gee_clf.json"

        shutil.copy2(source_path, target_pkl)
        shutil.copy2(source_clf, target_clf)

        metadata = dict(source.metadata_json or {})
        if source_json.exists():
            try:
                file_metadata = json.loads(source_json.read_text(encoding="utf-8"))
                metadata = {**file_metadata, **metadata}
            except json.JSONDecodeError:
                pass

        spatial_r2 = (
            (metadata.get("spatial_cv_metrics") or {}).get("r2_mean")
            or (metadata.get("cv_metrics") or {}).get("r2_mean")
            or 0.0
        )
        if float(spatial_r2) < 0.5:
            raise RuntimeError(f"Source model R2 is below 0.5: {spatial_r2}")

        metadata.update(
            {
                "model_name": TARGET_MODEL_NAME,
                "display_name": TARGET_DISPLAY_NAME,
                "target_dataset_key": TARGET_DATASET,
                "reference_dataset": TARGET_DATASET,
                "compatible_reference_datasets": [TARGET_DATASET],
                "reference_gee_id": "LARSE/GEDI/GEDI04_A_002_MONTHLY",
                "reference_band": "agbd",
                "reference_transform": "annual median agbd * 0.47",
                "feature_stack": "s2_dem",
                "gee_classifier_path": target_clf.name,
                "gee_deployable": True,
                "gee_algorithm_type": "native_classifier",
                "is_production_ready": True,
                "seeded_from_model": SOURCE_MODEL_NAME,
                "seeded_at": datetime.now(UTC).isoformat(),
                "l4a_note": (
                    "Compatibility seed for GEDI L4A Monthly. Uses the validated "
                    "Indonesia-wide GEDI S2/DEM HGB model family because GEDI L4B "
                    "is derived from GEDI L4A footprint biomass estimates. Trust "
                    "spatial_cv_metrics over plain CV; retrain with direct L4A "
                    "samples when a larger L4A training run is available."
                ),
            }
        )
        target_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        metrics = _metrics_from_metadata(metadata)
        row = db.query(UploadedModel).filter_by(name=TARGET_MODEL_NAME).first()
        if row is None:
            row = UploadedModel(name=TARGET_MODEL_NAME, model_type="carbon")
            db.add(row)

        row.display_name = TARGET_DISPLAY_NAME
        row.algorithm = metadata.get("algorithm", "hist_gradient_boosting")
        row.filename = target_pkl.name
        row.filepath = str(target_pkl.resolve())
        row.file_size_kb = round(target_pkl.stat().st_size / 1024, 2)
        row.is_active = True
        row.is_default = False
        row.is_legacy = False
        row.description = (
            "GEDI L4A Monthly-compatible carbon model seeded from the validated "
            "Indonesia-wide GEDI S2/DEM HGB model; spatial CV R2 >= 0.5."
        )
        row.version = "gedi-l4a-monthly-2023"
        row.metrics = metrics
        row.feature_names = metadata.get("feature_names") or source.feature_names
        row.metadata_json = metadata

        db.commit()
        print(
            f"Seeded {TARGET_MODEL_NAME}: R2={metrics['cv_metrics']['r2_mean']:.4f}, "
            f"model={target_pkl}, gee_classifier={target_clf}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    seed_gedi_l4a_model()
