"""Privileged MFA factors and single-use login challenges."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0050_admin_mfa"
down_revision = "0049_credential_probe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("admin_users", sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("admin_mfa",
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("secret_ciphertext", sa.Text()), sa.Column("recovery_hashes", JSONB(), nullable=False),
        sa.Column("last_counter", sa.BigInteger(), nullable=False, server_default="-1"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)), sa.Column("enrolled_at", sa.DateTime(timezone=True)))
    op.create_table("mfa_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("session_version", sa.Integer(), nullable=False), sa.Column("password_fingerprint", sa.String(64), nullable=False),
        sa.Column("pending_secret", sa.Text()), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_mfa_challenges_admin_id", "mfa_challenges", ["admin_id"])


def downgrade():
    op.drop_table("mfa_challenges")
    op.drop_table("admin_mfa")
    op.drop_column("admin_users", "mfa_enabled")
