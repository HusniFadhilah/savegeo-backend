"""Backfill the known Ilham Nugraha admin account name."""
from alembic import op
import sqlalchemy as sa


revision = "0038_backfill_ilham_nugraha_full_name"
down_revision = "0037_crop_monitoring_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE admin_users
            SET full_name = :full_name
            WHERE lower(username) = :username
              AND (full_name IS NULL OR trim(full_name) = '')
            """
        ).bindparams(
            full_name="Ilham Nugraha, S.T., M.M., IPM.",
            username="ilhamnugraha",
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE admin_users
            SET full_name = NULL
            WHERE lower(username) = :username
              AND full_name = :full_name
            """
        ).bindparams(
            full_name="Ilham Nugraha, S.T., M.M., IPM.",
            username="ilhamnugraha",
        )
    )
