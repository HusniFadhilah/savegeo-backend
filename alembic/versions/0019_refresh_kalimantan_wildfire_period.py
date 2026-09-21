"""Align the seeded Kalimantan wildfire event with its active monitoring period."""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "0019_kalimantan_period"
down_revision: str | None = "0018_persist_wildfire_hotspots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EVENT_SLUG = "kalimantan-2026"


def upgrade() -> None:
    events = sa.table(
        "disaster_events",
        sa.column("event_date", sa.Date()),
        sa.column("start_date", sa.Date()),
        sa.column("monitoring_from", sa.Date()),
        sa.column("slug", sa.String()),
    )
    op.execute(
        sa.update(events)
        .where(events.c.slug == EVENT_SLUG)
        .values(
            event_date=date(2026, 8, 20),
            start_date=date(2026, 8, 20),
            monitoring_from=date(2026, 8, 20),
        )
    )


def downgrade() -> None:
    events = sa.table(
        "disaster_events",
        sa.column("event_date", sa.Date()),
        sa.column("start_date", sa.Date()),
        sa.column("slug", sa.String()),
    )
    op.execute(
        sa.update(events)
        .where(events.c.slug == EVENT_SLUG)
        .values(event_date=date(2026, 1, 1), start_date=date(2026, 1, 1))
    )
