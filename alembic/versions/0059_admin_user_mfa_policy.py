"""Add per-account MFA requirement for admin users."""
from alembic import op
import sqlalchemy as sa

revision = "0059_admin_user_mfa_policy"
down_revision = "0058_uploaded_model_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "admin_users",
        sa.Column("mfa_required", sa.Boolean(), nullable=True),
    )


def downgrade():
    op.drop_column("admin_users", "mfa_required")
