"""Cache identical carbon analysis jobs for faster repeat loads."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0033_carbon_inference_cache"
down_revision = "0032_session_revocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "carbon_inference_cache",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("owner_key", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
    )
    op.create_index(
        "ix_carbon_inference_cache_owner_status", "carbon_inference_cache", ["owner_key", "status"]
    )
    op.create_index("ix_carbon_inference_cache_expires_at", "carbon_inference_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_carbon_inference_cache_expires_at", table_name="carbon_inference_cache")
    op.drop_index("ix_carbon_inference_cache_owner_status", table_name="carbon_inference_cache")
    op.drop_table("carbon_inference_cache")
