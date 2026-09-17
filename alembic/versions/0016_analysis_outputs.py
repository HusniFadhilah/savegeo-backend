"""Create immutable geospatial action output registry."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_analysis_outputs"
down_revision: str | None = "0015_wildfire_event_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_outputs",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("action_id", sa.String(64), sa.ForeignKey("analysis_provenance.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("profile", sa.String(128)),
        sa.Column("href", sa.Text(), nullable=False),
        sa.Column("storage_location", sa.Text()),
        sa.Column("checksum", sa.String(128)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("feature_count", sa.Integer()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_analysis_outputs_action_id", "analysis_outputs", ["action_id"])
    op.create_index("ix_analysis_outputs_status", "analysis_outputs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_analysis_outputs_status", table_name="analysis_outputs")
    op.drop_index("ix_analysis_outputs_action_id", table_name="analysis_outputs")
    op.drop_table("analysis_outputs")
