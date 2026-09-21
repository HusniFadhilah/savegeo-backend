"""Merge the legacy and canonical wildfire migration branches."""

from collections.abc import Sequence

revision: str = "0020_merge_wildfire_heads"
down_revision: tuple[str, str] = (
    "0017_seed_kalimantan_wildfire",
    "0019_kalimantan_period",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
