"""Scope chatbot history to an authenticated app viewer."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_chat_session_owner"
down_revision: str | None = "0027_viewer_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("owner_key", sa.String(length=80), nullable=True))
    op.create_index("ix_chat_sessions_owner_key", "chat_sessions", ["owner_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_owner_key", table_name="chat_sessions")
    op.drop_column("chat_sessions", "owner_key")
