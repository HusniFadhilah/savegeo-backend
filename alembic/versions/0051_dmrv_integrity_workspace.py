"""Seven project-integrity workflows; API-only access with retained revisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0051_dmrv_integrity_workspace"
down_revision = "0050_admin_mfa"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dmrv_integrity_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("record_key", sa.String(100), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=False),
        sa.Column("reviewed_by", sa.String(32)),
        sa.Column("review_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "category", "record_key", name="uq_dmrv_integrity_key"),
    )
    op.create_index("ix_dmrv_integrity_records_project_id", "dmrv_integrity_records", ["project_id"])
    op.create_table(
        "dmrv_integrity_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "record_id",
            sa.String(36),
            sa.ForeignKey("dmrv_integrity_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("record_id", "revision", name="uq_dmrv_integrity_revision"),
    )
    op.create_index("ix_dmrv_integrity_revisions_record_id", "dmrv_integrity_revisions", ["record_id"])
    op.create_table(
        "dmrv_integrity_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dmrv_integrity_documents_project_id", "dmrv_integrity_documents", ["project_id"])
    op.create_table(
        "dmrv_risk_policies",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("interval_days", sa.Integer, nullable=False),
        sa.Column("lookback_days", sa.Integer, nullable=False),
        sa.Column("tree_loss_threshold_ha", sa.Float, nullable=False),
        sa.Column("min_observations", sa.Integer, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dmrv_risk_policies_next_run_at", "dmrv_risk_policies", ["next_run_at"])
    if op.get_bind().dialect.name == "postgresql":
        # SaveGeo uses its own JWTs and membership checks in FastAPI. These
        # tables must not be directly exposed through Supabase's Data API.
        for table in (
            "dmrv_integrity_records",
            "dmrv_integrity_revisions",
            "dmrv_integrity_documents",
            "dmrv_risk_policies",
        ):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
            for role in ("anon", "authenticated"):
                op.execute(
                    f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN REVOKE ALL ON {table} FROM {role}; END IF; END $$"
                )


def downgrade():
    for table in (
        "dmrv_risk_policies",
        "dmrv_integrity_documents",
        "dmrv_integrity_revisions",
        "dmrv_integrity_records",
    ):
        op.drop_table(table)
