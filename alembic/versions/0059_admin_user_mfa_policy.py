"""Add per-account MFA requirement for admin users."""
from alembic import op

revision = "0059_admin_user_mfa_policy"
down_revision = "0058_uploaded_model_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    # Some installations applied this additive hotfix before their migration
    # chain was reconciled. Keep the canonical migration safe to replay there.
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS mfa_required BOOLEAN NULL")


def downgrade():
    op.drop_column("admin_users", "mfa_required")
