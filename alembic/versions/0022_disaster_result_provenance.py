"""Store capability validation and provenance on persisted disaster results."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_disaster_result_provenance"
down_revision: str | None = "0021_filter_kalimantan_hotspots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_results", sa.Column("provenance", postgresql.JSONB(), nullable=True))
    op.add_column("analysis_results", sa.Column("validation_status", sa.String(length=30), nullable=True))
    op.add_column("analysis_results", sa.Column("limitations", postgresql.JSONB(), nullable=True))
    op.add_column("analysis_results", sa.Column("stale_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_results", "stale_reason")
    op.drop_column("analysis_results", "limitations")
    op.drop_column("analysis_results", "validation_status")
    op.drop_column("analysis_results", "provenance")
