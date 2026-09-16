from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CarbonCalibrationDataset(Base):
    __tablename__ = "carbon_calibration_datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    access_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="restricted")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    validation_report: Mapped[dict | None] = mapped_column(JSONB)
    provenance: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.UTC),
        onupdate=lambda: dt.datetime.now(dt.UTC),
    )

    files: Mapped[list[CarbonCalibrationFile]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="CarbonCalibrationFile.created_at"
    )
    plots: Mapped[list[FieldPlot]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="FieldPlot.plot_id"
    )

    def to_dict(self, include_children: bool = False) -> dict:
        result = {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "name": self.name,
            "manifest": self.manifest,
            "access": self.access_classification,
            "status": self.status,
            "is_active": self.is_active,
            "validation_report": self.validation_report,
            "provenance": self.provenance,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_children:
            result["files"] = [item.to_dict() for item in self.files]
            result["plots"] = [item.to_dict() for item in self.plots]
        return result


class CarbonCalibrationFile(Base):
    __tablename__ = "carbon_calibration_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_pk: Mapped[int] = mapped_column(
        ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), index=True
    )
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    dataset: Mapped[CarbonCalibrationDataset] = relationship(back_populates="files")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_name": self.source_name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "file_kind": self.file_kind,
            "metadata": self.metadata_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class FieldPlot(Base):
    __tablename__ = "carbon_field_plots"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_pk: Mapped[int] = mapped_column(
        ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), index=True
    )
    plot_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    geometry_original: Mapped[dict | None] = mapped_column(JSONB)
    geometry_normalized: Mapped[dict | None] = mapped_column(JSONB)
    crs: Mapped[str] = mapped_column(String(64), nullable=False, default="EPSG:32748")
    area_m2: Mapped[float | None] = mapped_column(Float)
    area_ha: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict | None] = mapped_column(JSONB)
    uncertainty: Mapped[dict | None] = mapped_column(JSONB)
    qa_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    source_document: Mapped[str | None] = mapped_column(String(255))
    source_table: Mapped[str | None] = mapped_column(String(255))
    survey_date: Mapped[str | None] = mapped_column(String(32))

    dataset: Mapped[CarbonCalibrationDataset] = relationship(back_populates="plots")
    trees: Mapped[list[FieldTree]] = relationship(back_populates="plot", cascade="all, delete-orphan")

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id,
            "geometry": self.geometry_normalized or self.geometry_original,
            "geometry_original": self.geometry_original,
            "geometry_normalized": self.geometry_normalized,
            "crs": self.crs,
            "area_m2": self.area_m2,
            "area_ha": self.area_ha,
            **(self.attributes or {}),
            "uncertainty": self.uncertainty,
            "qa_status": self.qa_status,
            "source_document": self.source_document,
            "source_table": self.source_table,
            "survey_date": self.survey_date,
        }


class FieldTree(Base):
    __tablename__ = "carbon_field_trees"

    id: Mapped[int] = mapped_column(primary_key=True)
    plot_pk: Mapped[int] = mapped_column(ForeignKey("carbon_field_plots.id", ondelete="CASCADE"), index=True)
    tree_id: Mapped[str] = mapped_column(String(64), nullable=False)
    species: Mapped[str | None] = mapped_column(String(255))
    circumference_cm: Mapped[float | None] = mapped_column(Float)
    dbh_cm: Mapped[float | None] = mapped_column(Float)
    agb_kg: Mapped[float | None] = mapped_column(Float)
    bgb_kg: Mapped[float | None] = mapped_column(Float)
    carbon_agb_kg: Mapped[float | None] = mapped_column(Float)
    carbon_bgb_kg: Mapped[float | None] = mapped_column(Float)
    formula_version: Mapped[str] = mapped_column(String(64), nullable=False, default="dahana-2026-v1")
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    plot: Mapped[FieldPlot] = relationship(back_populates="trees")

    def to_dict(self) -> dict:
        return {
            key: getattr(self, key)
            for key in (
                "tree_id",
                "species",
                "circumference_cm",
                "dbh_cm",
                "agb_kg",
                "bgb_kg",
                "carbon_agb_kg",
                "carbon_bgb_kg",
                "formula_version",
                "validation_status",
            )
        }


class CarbonCalibrationRun(Base):
    __tablename__ = "carbon_calibration_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_pk: Mapped[int] = mapped_column(
        ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    target_pool: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[dict | None] = mapped_column(JSONB)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict:
        return {
            key: getattr(self, key)
            for key in (
                "id",
                "status",
                "method",
                "target_pool",
                "parameters",
                "metrics",
                "artifact_path",
                "artifact_sha256",
                "error",
                "progress",
                "created_at",
                "completed_at",
            )
        } | {
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class CarbonCalibrationModel(Base):
    __tablename__ = "carbon_calibration_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    dataset_pk: Mapped[int] = mapped_column(
        ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("carbon_calibration_runs.id", ondelete="SET NULL"))
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_pool: Mapped[str] = mapped_column(String(64), nullable=False)
    target_unit: Mapped[str] = mapped_column(String(64), nullable=False, default="Mg C/ha")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="experimental", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    trained_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    dataset: Mapped[CarbonCalibrationDataset] = relationship()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "version": self.version,
            "target_pool": self.target_pool,
            "target_unit": self.target_unit,
            "status": self.status,
            "metadata": self.metadata_json,
            "artifact_sha256": self.artifact_sha256,
            "is_active": self.is_active,
            "dataset_id": self.dataset.dataset_id if self.dataset else self.dataset_pk,
            "run_id": self.run_id,
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
        }
