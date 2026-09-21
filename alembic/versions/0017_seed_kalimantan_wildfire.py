"""Compatibility marker for an older renamed Kalimantan seed revision.

Some installations recorded this revision before the canonical
``0017_seed_kalimantan_wildfire_event`` name was introduced. Keeping a no-op
marker lets Alembic resolve and merge those existing version rows safely.
"""

from collections.abc import Sequence

revision: str = "0017_seed_kalimantan_wildfire"
down_revision: str | None = "0016_analysis_outputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
