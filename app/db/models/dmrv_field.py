"""dMRV field data: permanent plots, repeat visits, tree measurements, import batches.

Plot identity (``SurveyPlot``), visit (``SurveyVisit``) and tree
(``TreeMeasurement.tree_code``) are separate so repeat measurements add rows
instead of overwriting history.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ImportBatch(Base):
    __tablename__ = "dmrv_import_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)  # atomic | partial
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # committed | partial | rejected
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SurveyPlot(Base):
    __tablename__ = "dmrv_survey_plots"
    __table_args__ = (UniqueConstraint("project_id", "plot_code", name="uq_dmrv_plot_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    plot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    area_m2: Mapped[float | None] = mapped_column(Float)
    area_basis: Mapped[str | None] = mapped_column(String(16))  # measured | assumed
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SurveyVisit(Base):
    __tablename__ = "dmrv_survey_visits"
    __table_args__ = (UniqueConstraint("plot_id", "visit_date", name="uq_dmrv_visit_plot_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plot_id: Mapped[str] = mapped_column(ForeignKey("dmrv_survey_plots.id", ondelete="CASCADE"), index=True)
    visit_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    date_precision: Mapped[str] = mapped_column(String(8), nullable=False, default="day")  # day | year
    operator: Mapped[str | None] = mapped_column(String(128))
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("dmrv_import_batches.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TreeMeasurement(Base):
    __tablename__ = "dmrv_tree_measurements"
    __table_args__ = (UniqueConstraint("visit_id", "tree_code", name="uq_dmrv_tree_visit_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    visit_id: Mapped[str] = mapped_column(ForeignKey("dmrv_survey_visits.id", ondelete="CASCADE"), index=True)
    tree_code: Mapped[str] = mapped_column(String(64), nullable=False)
    species: Mapped[str | None] = mapped_column(String(255))
    circumference_cm: Mapped[float | None] = mapped_column(Float)
    dbh_cm: Mapped[float | None] = mapped_column(Float)
    height_m: Mapped[float | None] = mapped_column(Float)
    biomass_kg: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="live")  # live|dead|recruit|missing
    biomass_source: Mapped[str | None] = mapped_column(String(16))  # reported
    qa_flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # The import batch that brought this tree in (a visit can receive trees from several batches).
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("dmrv_import_batches.id", ondelete="SET NULL"))
