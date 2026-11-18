from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.admin_user import AdminUser


class UploadedModel(Base):
    __tablename__ = "uploaded_models"
    __table_args__ = (
        Index("ix_models_active_type_id", "is_active", "model_type", "id"),
        Index("uq_uploaded_models_default_type", "model_type", unique=True,
              postgresql_where=text("is_default IS TRUE"), sqlite_where=text("is_default = 1")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_type: Mapped[str] = mapped_column(String(50), nullable=False)  # carbon|vegetation|landcover
    algorithm: Mapped[str | None] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    filepath: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_kb: Mapped[float | None] = mapped_column(Float)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="experimental")
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approval_digest: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(50))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    feature_names: Mapped[list | None] = mapped_column(JSONB)
    # Native JSONB (legacy stored this double-encoded as a Text column) — nothing outside
    # this codebase reads the raw column, so switching representation is safe.
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )

    uploader: Mapped[AdminUser | None] = relationship(
        "AdminUser", foreign_keys=lambda: [UploadedModel.uploaded_by], backref="uploaded_models"
    )

    def _base_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "model_type": self.model_type,
            "algorithm": self.algorithm,
            "filename": self.filename,
            "file_size_kb": self.file_size_kb,
            "is_default": self.is_default,
            "is_active": self.is_active,
            "is_legacy": self.is_legacy,
            "lifecycle_state": self.lifecycle_state,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "description": self.description,
            "version": self.version,
            "metrics": self.metrics,
            "feature_names": self.feature_names,
            "metadata_json": self.metadata_json,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_dict(self, include_path: bool = False) -> dict:
        data = self._base_dict()
        data["uploaded_by"] = self.uploader.username if self.uploader else None
        if include_path:
            data["filepath"] = self.filepath
        return data

    def to_dict_public(self, include_path: bool = False) -> dict:
        data = self._base_dict()
        # The application needs scientific descriptors, not operator metadata.
        data.pop("filename", None)
        data.pop("file_size_kb", None)
        data.pop("approved_by", None)
        data.pop("approved_at", None)
        public_keys = {
            "algorithm", "version", "created_at", "trained_at", "n_samples",
            "n_features", "feature_names", "train_metrics", "cv_metrics", "cv_method",
            "scaled_features", "feature_importance", "gee_deployable", "preliminary",
            "method", "training_domain", "training_mode", "reference_year", "provider",
            "builtin", "time_series_supported", "compatible_reference_datasets",
            "target_dataset", "target_pool", "units", "description", "limitations",
            "reference_dataset", "reference_datasets", "training_source", "pool",
        }
        data["metadata_json"] = {k: v for k, v in (self.metadata_json or {}).items() if k in public_keys}
        return data


class UploadedModelEvaluation(Base):
    __tablename__ = "uploaded_model_evaluations"
    __table_args__ = (Index("ix_model_evaluations_model_created", "model_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("uploaded_models.id", ondelete="CASCADE"), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_source: Mapped[str | None] = mapped_column(Text)
    dataset_storage_path: Mapped[str | None] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evaluated_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class UploadedModelReview(Base):
    __tablename__ = "uploaded_model_reviews"
    __table_args__ = (Index("ix_model_reviews_model_created", "model_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("uploaded_models.id", ondelete="CASCADE"), nullable=False)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("uploaded_model_evaluations.id", ondelete="RESTRICT"), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
