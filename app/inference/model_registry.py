"""Compatibility facade over the authoritative uploaded_models database table.

No runtime read, write or recovery path depends on registry.json. Existing
registry files can remain as offline historical records without activating code.
"""
from __future__ import annotations
from pathlib import Path

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal
from app.repositories.uploaded_model_repo import get_active_model_path


class ModelRegistry:
    def __init__(self, models_dir: str | None = None):
        self.models_dir = Path(models_dir) if models_dir else get_settings().model_path

    @property
    def registry(self):
        with SessionLocal() as db:
            rows = db.query(UploadedModel).filter_by(model_type="carbon", is_active=True).order_by(UploadedModel.id).limit(10000).all()
            return {"models": {row.name: {"path": row.filename, "metadata": row.metadata_json or {},
                "registered_at": row.uploaded_at.isoformat() if row.uploaded_at else None} for row in rows},
                "default": next((row.name for row in rows if row.is_default), None)}

    def get_model_path(self, model_name: str | None = None) -> str:
        with SessionLocal() as db:
            path = get_active_model_path(db, "carbon", model_name)
            if path is None:
                raise ValueError("An active registered model is required")
            return path

    def get_model_info(self, model_name: str | None = None) -> dict:
        with SessionLocal() as db:
            query = db.query(UploadedModel).filter_by(model_type="carbon", is_active=True)
            row = query.filter_by(name=model_name).first() if model_name else query.filter_by(is_default=True).first()
            if row is None:
                raise ValueError("An active registered model is required")
            return dict(row.metadata_json or {})

    def list_models(self) -> list[dict]:
        with SessionLocal() as db:
            return [row.to_dict_public() for row in db.query(UploadedModel).filter_by(
                model_type="carbon", is_active=True).order_by(UploadedModel.id).limit(10000)]

    def register_model(self, model_name, model_path, metadata, set_as_default=False):
        import hashlib
        path = Path(model_path).with_suffix(".onnx").resolve()
        if not path.is_relative_to(get_settings().model_path.resolve()) or not path.is_file():
            raise ValueError("A local ONNX export in MODEL_DIR is required")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if metadata.get("artifact_sha256") != digest:
            raise ValueError("Exported model hash does not match metadata")
        with SessionLocal() as db:
            row = db.query(UploadedModel).filter_by(name=model_name).first()
            if row is None:
                row = UploadedModel(name=model_name, display_name=model_name, model_type="carbon",
                    filename=path.name, filepath=str(path), is_active=False)
                db.add(row)
            row.filename, row.filepath = path.name, str(path)
            row.metadata_json, row.artifact_sha256 = dict(metadata), digest
            row.feature_names, row.algorithm = metadata.get("feature_names", []), metadata.get("algorithm")
            # A new export always requires administrator review, including a
            # replacement under an existing name. No training-side activation.
            row.is_active, row.is_default = False, False
            db.commit()
        if set_as_default:
            raise ValueError("Model registered inactive; review and activate it in administration first")

    def set_default(self, model_name):
        with SessionLocal() as db:
            row = db.query(UploadedModel).filter_by(name=model_name, is_active=True).first()
            if row is None:
                raise ValueError("An active model is required")
            db.query(UploadedModel).filter_by(model_type=row.model_type).update({"is_default": False})
            row.is_default = True
            db.commit()

    def delete_model(self, model_name):
        with SessionLocal() as db:
            row = db.query(UploadedModel).filter_by(name=model_name).first()
            if row is None:
                raise ValueError("Model not found")
            # Retain the artifact and audit history; disable runtime selection.
            row.is_active, row.is_default = False, False
            db.commit()
