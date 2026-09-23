"""Store explicit parameters used by persisted disaster runs.

Revision ID: 0024_analysis_run_parameters
Revises: 0023_satellite_scene_id
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024_analysis_run_parameters"
down_revision = "0023_satellite_scene_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_runs", sa.Column("parameters", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_runs", "parameters")
