"""Share security quotas across workers without changing application data."""
from alembic import op
import sqlalchemy as sa

revision = "0053_shared_rate_limits"
down_revision = "0052_field_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("request_rate_buckets",
        sa.Column("bucket_key", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.BigInteger(), primary_key=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False))
    op.create_index("ix_request_rate_buckets_expires_at", "request_rate_buckets", ["expires_at"])


def downgrade():
    op.drop_index("ix_request_rate_buckets_expires_at", table_name="request_rate_buckets")
    op.drop_table("request_rate_buckets")
