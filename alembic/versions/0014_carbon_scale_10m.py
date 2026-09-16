"""Use 10 m as the default carbon analysis scale.

Only values produced by the previous 100 m default migration are changed.
Deliberate administrator overrides remain untouched.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "0014_carbon_scale_10m"
down_revision: str | None = "0013_carbon_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE system_config
            SET value = '10'
            WHERE key = 'analysis.carbon_scale' AND value = '100'
            """
        )
    )


def downgrade() -> None:
    # Do not overwrite a value that may have been intentionally changed after
    # this migration. The admin configuration editor can restore 100 m.
    pass
