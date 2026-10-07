"""Add admin-managed raw imagery provider catalog overlays."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_imagery_provider_entries"
down_revision: str | None = "0028_chat_session_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "imagery_provider_entries",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("overrides", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("imagery_provider_entries")
