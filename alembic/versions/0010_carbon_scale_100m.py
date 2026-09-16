"""Use a finer default scale for carbon analysis.

Only the old seeded value is migrated. A value changed deliberately by an
administrator is preserved.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "0010_carbon_scale_100m"
down_revision: str | None = "0009_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE system_config
            SET value = '100'
            WHERE key = 'analysis.carbon_scale' AND value = '250'
            """
        )
    )


def downgrade() -> None:
    # Do not overwrite a value that may have been intentionally changed after
    # this migration. The original 250 m value remains available via the
    # admin configuration editor if an operator needs to restore it.
    pass
