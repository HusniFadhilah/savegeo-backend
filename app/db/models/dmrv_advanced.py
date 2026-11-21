"""Append-only reported credit events and mobile field observations."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvCreditEvent(Base):
    __tablename__ = "dmrv_credit_events"
    __table_args__ = (
        UniqueConstraint("project_id", "event_id", name="uq_dmrv_credit_event_project_id"),
        UniqueConstraint("project_id", "registry", "external_transaction_id", name="uq_dmrv_credit_external_tx"),
        CheckConstraint("quantity > 0", name="ck_dmrv_credit_quantity_positive"),
        CheckConstraint(
            "event_type IN ('issuance_reported','transfer_reported','use_reported','retirement_reported','cancellation_reported')",
            name="ck_dmrv_credit_event_type",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="RESTRICT"), index=True)
    batch_reference: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_from: Mapped[str | None] = mapped_column(String(200))
    owner_to: Mapped[str | None] = mapped_column(String(200))
    registry: Mapped[str] = mapped_column(String(100), nullable=False)
    external_transaction_id: Mapped[str] = mapped_column(String(160), nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DmrvFieldCapture(Base):
    __tablename__ = "dmrv_field_captures"
    __table_args__ = (
        UniqueConstraint("project_id", "capture_id", name="uq_dmrv_field_capture_project_id"),
        CheckConstraint("latitude BETWEEN -85 AND 85", name="ck_dmrv_field_capture_lat"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_dmrv_field_capture_lon"),
    )

    capture_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    plot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    plot_id: Mapped[str | None] = mapped_column(ForeignKey("dmrv_survey_plots.id", ondelete="SET NULL"), index=True)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    gps_accuracy_m: Mapped[float | None] = mapped_column(Float)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    photo_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("dmrv_integrity_documents.id", ondelete="SET NULL")
    )
    qa_flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
