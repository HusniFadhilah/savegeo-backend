"""Safe credential connection-check metadata; no invented key expiry."""
from alembic import op
import sqlalchemy as sa

revision = "0049_credential_probe"
down_revision = "0048_workspace_analysis"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gee_credentials", sa.Column("last_checked_at", sa.DateTime(timezone=True)))
    op.add_column("gee_credentials", sa.Column("last_check_status", sa.String(16)))
    op.add_column("gee_credentials", sa.Column("last_check_code", sa.String(64)))


def downgrade():
    for name in ("last_check_code", "last_check_status", "last_checked_at"):
        op.drop_column("gee_credentials", name)
