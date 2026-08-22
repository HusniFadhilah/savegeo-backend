from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.admin_user import AdminUser


class UploadedModel(Base):
    __tablename__ = "uploaded_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_type: Mapped[str] = mapped_column(String(50), nullable=False)  # carbon|vegetation|landcover
    algorithm: Mapped[str | None] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    filepath: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_kb: Mapped[float | None] = mapped_column(Float)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_legacy: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(50))
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    feature_names: Mapped[list | None] = mapped_column(JSONB)
    # Native JSONB (legacy stored this double-encoded as a Text column) — nothing outside
    # this codebase reads the raw column, so switching representation is safe.
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )

    uploader: Mapped[AdminUser | None] = relationship("AdminUser", backref="uploaded_models")

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
        if include_path:
            data["filepath"] = self.filepath
        return data
