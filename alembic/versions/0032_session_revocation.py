"""Durable account session revocation for VAPT logout remediation.

Deploy before the new application. Old JWTs intentionally require a fresh login.
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_session_revocation"
down_revision = "0031_indonesia_wildfire_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("users", "admin_users"):
        op.add_column(table, sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    for table in ("admin_users", "users"):
        op.drop_column(table, "session_version")
