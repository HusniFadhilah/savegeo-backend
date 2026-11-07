"""Versioned dMRV submission locks and review findings."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0044_dmrv_submissions_review"
down_revision = "0043_dmrv_evidence_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmrv_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("evidence_package_id", sa.String(36), sa.ForeignKey("dmrv_evidence_packages.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("locked_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("locked_manifest_hash", sa.String(64), nullable=False),
        sa.Column("locked_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("reviewer_key", sa.String(32)),
        sa.Column("supersedes_id", sa.String(36), sa.ForeignKey("dmrv_submissions.id", ondelete="RESTRICT")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("decision_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "version", name="uq_dmrv_submission_project_version"),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'under_review', 'changes_requested', 'approved', 'rejected', 'superseded')",
            name="ck_dmrv_submission_status",
        ),
    )
    op.create_index("ix_dmrv_submissions_project_id", "dmrv_submissions", ["project_id"])
    op.create_table(
        "dmrv_review_findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("submission_id", sa.String(36), sa.ForeignKey("dmrv_submissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("due_date", sa.Date()),
        sa.Column("response", sa.Text()),
        sa.Column("evidence_references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("responded_by", sa.String(32)),
        sa.Column("resolved_by", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('open', 'responded', 'resolved')", name="ck_dmrv_review_finding_status"),
    )
    op.create_index("ix_dmrv_review_findings_submission_id", "dmrv_review_findings", ["submission_id"])


def downgrade() -> None:
    op.drop_index("ix_dmrv_review_findings_submission_id", table_name="dmrv_review_findings")
    op.drop_table("dmrv_review_findings")
    op.drop_index("ix_dmrv_submissions_project_id", table_name="dmrv_submissions")
    op.drop_table("dmrv_submissions")
