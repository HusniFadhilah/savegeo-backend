"""Repair the public visibility flag for already-published disaster events.

Older ingestion/publish scripts changed ``status`` directly and left
``is_public`` false. The public catalog intentionally requires both values,
so those events disappeared even though the admin table showed them as
published.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_repair_public_visibility"
down_revision: str | None = "0024_analysis_run_parameters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE disaster_events
            SET is_public = (status = 'published')
            WHERE is_public IS DISTINCT FROM (status = 'published')
            """
        )
    )


def downgrade() -> None:
    # This is a corrective data migration; restoring the inconsistent flags
    # would reintroduce the public catalog defect.
    pass
