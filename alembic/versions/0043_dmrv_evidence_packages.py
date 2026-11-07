"""Persist reproducible dMRV evidence package manifests."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0043_dmrv_evidence_packages"
down_revision = "0042_dmrv_monitoring_periods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmrv_evidence_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("dmrv_snapshots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("principal_key", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("missing_components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("readiness", sa.String(16), nullable=False, server_default="incomplete"),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("principal_key", "idempotency_key", name="uq_dmrv_evidence_idempotency"),
        sa.CheckConstraint("readiness IN ('incomplete', 'ready')", name="ck_dmrv_evidence_readiness"),
    )
    op.create_index("ix_dmrv_evidence_packages_project_id", "dmrv_evidence_packages", ["project_id"])
    op.create_index("ix_dmrv_evidence_packages_snapshot_id", "dmrv_evidence_packages", ["snapshot_id"])
    op.create_index("ix_dmrv_evidence_project_created", "dmrv_evidence_packages", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dmrv_evidence_project_created", table_name="dmrv_evidence_packages")
    op.drop_index("ix_dmrv_evidence_packages_snapshot_id", table_name="dmrv_evidence_packages")
    op.drop_index("ix_dmrv_evidence_packages_project_id", table_name="dmrv_evidence_packages")
    op.drop_table("dmrv_evidence_packages")
