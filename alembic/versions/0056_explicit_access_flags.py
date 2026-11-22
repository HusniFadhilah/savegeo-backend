"""Make existing admin access explicit; new unassigned accounts deny by default."""
from alembic import op
import sqlalchemy as sa

revision = "0056_explicit_access_flags"
down_revision = "0055_durable_analysis_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("admin_users", sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Preserve precisely the access of accounts existing at migration time.
    # A missing role on accounts created afterward no longer grants access.
    op.execute("UPDATE admin_users SET is_superuser = true WHERE role_id IS NULL")
    op.alter_column("admin_users", "is_superuser", server_default=None)
    op.add_column("roles", sa.Column("is_viewer", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE roles SET is_viewer = true WHERE id = (SELECT min(id) FROM roles WHERE lower(name) = 'viewer')")
    op.alter_column("roles", "is_viewer", server_default=None)


def downgrade():
    op.drop_column("roles", "is_viewer")
    op.drop_column("admin_users", "is_superuser")
