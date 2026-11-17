"""Persist analysis jobs without modifying existing users, data or job files."""
from alembic import op
import sqlalchemy as sa

revision = "0055_durable_analysis_jobs"
down_revision = "0054_provider_cache"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("analysis_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("cache_key", sa.String(64)),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_expires_at", sa.Float()),
        sa.Column("heartbeat_at", sa.Float()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("owner", "idempotency_key", name="uq_analysis_jobs_idempotency"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_analysis_jobs_attempts"))
    op.create_index("ix_analysis_jobs_claim", "analysis_jobs", ["status", "created_at"])
    op.create_index("ix_analysis_jobs_lease", "analysis_jobs", ["lease_expires_at"])
    op.create_index("ix_analysis_jobs_owner", "analysis_jobs", ["owner", "created_at"])


def downgrade():
    op.drop_table("analysis_jobs")
