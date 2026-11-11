"""Persist green rankings per year, period, administrative scope and region."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0046_green_ranking_snapshots"
down_revision = "0045_carbon_field_observations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "green_ranking_snapshots",
        sa.Column("scope_key", sa.String(length=64), primary_key=True),
        sa.Column("source_run_id", sa.String(length=36), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("statistics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("boundary_source", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_due_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_green_ranking_snapshot_freshness", "green_ranking_snapshots", ["refreshed_at"]
    )
    op.create_table(
        "green_ranking_snapshot_rows",
        sa.Column(
            "scope_key",
            sa.String(length=64),
            sa.ForeignKey("green_ranking_snapshots.scope_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("code", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("green_percent", sa.Float(), nullable=True),
        sa.Column("coverage_percent", sa.Float(), nullable=True),
        sa.Column("area_ha", sa.Float(), nullable=True),
        sa.Column("valid_area_ha", sa.Float(), nullable=True),
        sa.Column("green_area_ha", sa.Float(), nullable=True),
        sa.Column("trees_ha", sa.Float(), nullable=True),
        sa.Column("grass_ha", sa.Float(), nullable=True),
        sa.Column("crops_ha", sa.Float(), nullable=True),
        sa.Column("shrubs_ha", sa.Float(), nullable=True),
        sa.Column("flooded_ha", sa.Float(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index(
        "ix_green_ranking_snapshot_rows_eligible",
        "green_ranking_snapshot_rows",
        ["eligible"],
    )
    op.create_index(
        "ix_green_ranking_snapshot_rank",
        "green_ranking_snapshot_rows",
        ["scope_key", "eligible", "rank"],
    )
    op.create_index(
        "ix_green_ranking_snapshot_green",
        "green_ranking_snapshot_rows",
        ["scope_key", "eligible", "green_percent"],
    )


def downgrade():
    op.drop_table("green_ranking_snapshot_rows")
    op.drop_index("ix_green_ranking_snapshot_freshness", table_name="green_ranking_snapshots")
    op.drop_table("green_ranking_snapshots")
