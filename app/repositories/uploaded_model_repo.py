from __future__ import annotations

import json
import hashlib
import hmac
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel

ALLOWED_MODEL_EXTS = {".onnx"}


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


def _verified_database_artifact(model: UploadedModel, path: Path) -> bool:
    expected = model.artifact_sha256
    if not isinstance(expected, str) or len(expected) != 64 or path.stat().st_size > 128 * 1024 * 1024:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return hmac.compare_digest(digest.hexdigest(), expected)


def get_active_model_path(db: Session, model_type: str, model_name: str | None = None) -> str | None:
    """Resolve which model file to load for inference.

    Only DB-registered active artifacts may be executed. A bare filename must
    not bypass the upload review and activation process.
    """
    settings = get_settings()
    root = settings.model_path.resolve()

    def safe_model_file(path: Path) -> str | None:
        if path.suffix.lower() in {".pkl", ".joblib"}:
            path = path.with_suffix(".onnx")
        if path.is_symlink():
            return None
        resolved = path.resolve()
        if resolved.is_relative_to(root) and resolved.is_file() and resolved.suffix.lower() in ALLOWED_MODEL_EXTS:
            return str(resolved)
        return None

    if model_name:
        m = db.query(UploadedModel).filter_by(name=model_name, is_active=True).first()
    else:
        m = db.query(UploadedModel).filter_by(model_type=model_type, is_default=True, is_active=True).first()

    if m:
        registered_path = Path(m.filepath)
        safe_path = safe_model_file(registered_path)
        if safe_path and _verified_database_artifact(m, Path(safe_path)):
            return safe_path

        # Database rows created on Windows or by the legacy app can contain
        # an absolute path that does not exist inside the Linux production
        # container. The filename is the portable identity of the artifact;
        # resolve it from the mounted production model directory as a fallback.
        portable_path = settings.model_path / m.filename
        safe_path = safe_model_file(portable_path)
        if safe_path and _verified_database_artifact(m, Path(safe_path)):
            return safe_path

    return None


def import_legacy_models(db: Session, saved_models_dir: str) -> dict:
    """Scan a legacy `saved_models/` directory for model files not yet in the DB and
    register them with `is_legacy=True`. Skips ArcGIS Living Atlas models (those are
    served directly from the registry, not via UploadedModel).
    """
    saved_dir = Path(saved_models_dir)
    if not saved_dir.exists():
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": []}

    imported = 0
    updated = 0
    skipped = 0
    errors: list[dict] = []

    for model_file in saved_dir.iterdir():
        if model_file.suffix.lower() not in ALLOWED_MODEL_EXTS:
            continue
        if model_file.is_symlink() or not model_file.resolve().is_relative_to(saved_dir.resolve()):
            skipped += 1
            continue
        try:
            model_name = model_file.stem
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
            digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
            if metadata.get("artifact_sha256") != digest:
                raise ValueError("Model export integrity does not match its metadata")
            file_size_kb = round(model_file.stat().st_size / 1024, 2)

            existing = db.query(UploadedModel).filter_by(name=model_name).first()
            if existing is not None:
                # Runtime model files live outside Git and can be replaced by a
                # retraining/export job without changing the model name. Keep
                # the DB registry in lock-step with the sidecar metadata while
                # preserving administrator-controlled active/default flags.
                if existing.artifact_sha256 and existing.artifact_sha256 != digest:
                    existing.is_active, existing.is_default = False, False
                existing.artifact_sha256 = digest
                existing.display_name = existing.display_name or model_name
                existing.algorithm = metadata.get("algorithm") or existing.algorithm
                existing.filename = model_file.name
                existing.filepath = str(model_file.resolve())
                existing.file_size_kb = file_size_kb
                existing.version = metadata.get("version") or existing.version
                existing.metrics = metrics
                existing.feature_names = metadata.get("feature_names", existing.feature_names or [])
                existing.metadata_json = metadata or existing.metadata_json
                updated += 1
                continue

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
                    is_active=False,
                    is_legacy=True,
                    description=f"Imported from legacy saved_models: {model_file.name}",
                    version=metadata.get("version"),
                    metrics=metrics,
                    feature_names=metadata.get("feature_names", []),
                    metadata_json=metadata or None,
                    artifact_sha256=digest,
                    uploaded_by=None,
                )
            )
            imported += 1
        except Exception as e:  # noqa: BLE001 - collect per-file errors, keep importing the rest
            errors.append({"file": str(model_file), "error": str(e)})

    db.commit()
    return {"imported": imported, "updated": updated, "skipped": skipped, "errors": errors}
