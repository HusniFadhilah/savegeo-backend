"""Persist explicit coverage, quality flags, and review reasons."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic's default version_num column is VARCHAR(32); keep revision IDs within it.
revision: str = "0026_disaster_quality"
down_revision: str | None = "0025_repair_public_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_results", sa.Column("coverage", postgresql.JSONB(), nullable=True))
    op.add_column("analysis_results", sa.Column("quality_flags", postgresql.JSONB(), nullable=True))
    op.add_column("analysis_results", sa.Column("review_reasons", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_results", "review_reasons")
    op.drop_column("analysis_results", "quality_flags")
    op.drop_column("analysis_results", "coverage")
