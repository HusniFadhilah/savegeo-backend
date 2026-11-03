"""Cache independent crop-monitoring sections without retaining expired map IDs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0037_crop_monitoring_cache"
down_revision = "0036_carbon_annual_estimates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crop_monitoring_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("field_id", sa.Integer(), sa.ForeignKey("fields.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section", sa.String(40), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_crop_monitoring_cache_field_id", "crop_monitoring_cache", ["field_id"])
    op.create_index("ix_crop_monitoring_cache_expiry", "crop_monitoring_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_crop_monitoring_cache_expiry", table_name="crop_monitoring_cache")
    op.drop_index("ix_crop_monitoring_cache_field_id", table_name="crop_monitoring_cache")
    op.drop_table("crop_monitoring_cache")
