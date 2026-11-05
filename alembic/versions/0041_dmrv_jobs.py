"""dMRV durable jobs and calculation snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0041_dmrv_jobs"
down_revision = "0040_dmrv_field_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmrv_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("progress", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(64)),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("created_by", "idempotency_key", name="uq_dmrv_job_idempotency"),
    )
    op.create_index("ix_dmrv_jobs_project_id", "dmrv_jobs", ["project_id"])
    op.create_index("ix_dmrv_jobs_status_run_after", "dmrv_jobs", ["status", "run_after"])
    op.create_table(
        "dmrv_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("dmrv_jobs.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.Column("outputs", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "snapshot_hash", name="uq_dmrv_snapshot_hash"),
    )
    op.create_index("ix_dmrv_snapshots_project_id", "dmrv_snapshots", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_dmrv_snapshots_project_id", table_name="dmrv_snapshots")
    op.drop_table("dmrv_snapshots")
    op.drop_index("ix_dmrv_jobs_status_run_after", table_name="dmrv_jobs")
    op.drop_index("ix_dmrv_jobs_project_id", table_name="dmrv_jobs")
    op.drop_table("dmrv_jobs")
