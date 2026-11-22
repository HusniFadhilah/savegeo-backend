"""Add shared provider cache; preserve all existing application records."""
from alembic import op
import sqlalchemy as sa

revision = "0054_provider_cache"
down_revision = "0053_shared_rate_limits"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("provider_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("value", sa.JSON()),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("next_request_at", sa.Float(), nullable=False))
    op.create_index("ix_provider_cache_expires_at", "provider_cache", ["expires_at"])


def downgrade():
    op.drop_index("ix_provider_cache_expires_at", table_name="provider_cache")
    op.drop_table("provider_cache")
