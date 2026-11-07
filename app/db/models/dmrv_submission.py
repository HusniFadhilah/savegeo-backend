"""Versioned immutable submission locks and independent-review findings."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DmrvSubmission(Base):
    __tablename__ = "dmrv_submissions"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_dmrv_submission_project_version"),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'under_review', 'changes_requested', 'approved', 'rejected', 'superseded')",
            name="ck_dmrv_submission_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("dmrv_projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_package_id: Mapped[str] = mapped_column(ForeignKey("dmrv_evidence_packages.id", ondelete="RESTRICT"), nullable=False)
    locked_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locked_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locked_inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_key: Mapped[str | None] = mapped_column(String(32))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("dmrv_submissions.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class DmrvReviewFinding(Base):
    __tablename__ = "dmrv_review_findings"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'responded', 'resolved')", name="ck_dmrv_review_finding_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("dmrv_submissions.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    response: Mapped[str | None] = mapped_column(Text)
    evidence_references: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    responded_by: Mapped[str | None] = mapped_column(String(32))
    resolved_by: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
