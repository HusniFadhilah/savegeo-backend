"""Persist annual carbon statistics independently of temporary map IDs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0036_carbon_annual_estimates"
down_revision = "0035_account_full_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "carbon_annual_estimates",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("analysis_year", sa.Integer(), nullable=False),
        sa.Column("reference_year", sa.Integer(), nullable=False),
        sa.Column("dataset_key", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("aoi_checksum", sa.String(64), nullable=False),
        sa.Column("observed_through", sa.Date(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_carbon_annual_estimates_year_pool", "carbon_annual_estimates", ["analysis_year", "dataset_key"])


def downgrade() -> None:
    op.drop_index("ix_carbon_annual_estimates_year_pool", table_name="carbon_annual_estimates")
    op.drop_table("carbon_annual_estimates")
