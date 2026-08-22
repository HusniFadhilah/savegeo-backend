from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel

ALLOWED_MODEL_EXTS = {".pkl", ".joblib", ".h5", ".pt", ".pth", ".onnx", ".bin"}


def infer_model_type_from_name(filename: str) -> str:
    name = filename.lower()
    if "carbon" in name:
        return "carbon"
    if "vegetation" in name or "veg" in name:
        return "vegetation"
    if "landcover" in name or "land_cover" in name:
        return "landcover"
    return "carbon"


def _safe_json_load_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - malformed/missing legacy registry file: treat as empty, not a crash
        return {}


def get_active_model_path(db: Session, model_type: str, model_name: str | None = None) -> str | None:
    """Resolve which model file to load for inference.

    Mirrors legacy `admin_routes.get_active_model_path`: prefer the DB-registered
    active/default `UploadedModel` row; fall back to a bare file lookup in MODEL_DIR
    for models that predate the DB registry.
    """
    settings = get_settings()
    if model_name:
        m = db.query(UploadedModel).filter_by(name=model_name, is_active=True).first()
    else:
        m = db.query(UploadedModel).filter_by(model_type=model_type, is_default=True, is_active=True).first()

    if m and Path(m.filepath).exists():
        return str(Path(m.filepath).resolve())

    if model_name:
        for ext in ALLOWED_MODEL_EXTS:
            candidate = settings.model_path / f"{model_name}{ext}"
            if candidate.exists():
                return str(candidate.resolve())

    return None


def import_legacy_models(db: Session, saved_models_dir: str) -> dict:
    """Scan a legacy `saved_models/` directory for model files not yet in the DB and
    register them with `is_legacy=True`. Skips ArcGIS Living Atlas models (those are
    served directly from the registry, not via UploadedModel).
    """
    saved_dir = Path(saved_models_dir)
    if not saved_dir.exists():
        return {"imported": 0, "skipped": 0, "errors": []}

    imported = 0
    skipped = 0
    errors: list[dict] = []

    for model_file in saved_dir.iterdir():
        if model_file.suffix.lower() not in ALLOWED_MODEL_EXTS:
            continue
        try:
            model_name = model_file.stem
            if db.query(UploadedModel).filter_by(name=model_name).first():
                skipped += 1
                continue

            metadata = _safe_json_load_file(model_file.with_suffix(".json"))
            if metadata.get("training_source") == "arcgis_living_atlas":
                skipped += 1
                continue

            model_type = infer_model_type_from_name(model_file.name)
            metrics = {
                "train_metrics": metadata.get("train_metrics", {}),
                "cv_metrics": metadata.get("cv_metrics", {}),
                "n_samples": metadata.get("n_samples"),
                "n_features": metadata.get("n_features"),
                "trained_at": metadata.get("trained_at"),
                "scaled_features": metadata.get("scaled_features"),
            }
            file_size_kb = round(model_file.stat().st_size / 1024, 2)

            db.add(
                UploadedModel(
                    name=model_name,
                    display_name=model_name,
                    model_type=model_type,
                    algorithm=metadata.get("algorithm"),
                    filename=model_file.name,
                    filepath=str(model_file.resolve()),
                    file_size_kb=file_size_kb,
                    is_default=False,
                    is_active=True,
                    is_legacy=True,
                    description=f"Imported from legacy saved_models: {model_file.name}",
                    version=metadata.get("version"),
                    metrics=metrics,
                    feature_names=metadata.get("feature_names", []),
                    metadata_json=metadata or None,
                    uploaded_by=None,
                )
            )
            imported += 1
        except Exception as e:  # noqa: BLE001 - collect per-file errors, keep importing the rest
            errors.append({"file": str(model_file), "error": str(e)})

    db.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}
