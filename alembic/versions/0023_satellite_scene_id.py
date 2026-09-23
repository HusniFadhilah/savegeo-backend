"""Keep source scene identifiers for imagery idempotency and provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_satellite_scene_id"
down_revision: str | None = "0022_disaster_result_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("satellite_imagery", sa.Column("scene_id", sa.String(length=255), nullable=True))
    op.create_index("ix_satellite_imagery_scene_id", "satellite_imagery", ["scene_id"])


def downgrade() -> None:
    op.drop_index("ix_satellite_imagery_scene_id", table_name="satellite_imagery")
    op.drop_column("satellite_imagery", "scene_id")
