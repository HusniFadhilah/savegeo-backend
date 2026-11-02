"""Add editable full names without changing existing account identifiers."""
from alembic import op
import sqlalchemy as sa

revision = "0035_account_full_names"
down_revision = "0034_national_wildfire_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("users", "admin_users"):
        op.add_column(table, sa.Column("full_name", sa.String(160), nullable=True))


def downgrade() -> None:
    for table in ("admin_users", "users"):
        op.drop_column(table, "full_name")
