"""Hide persisted observations outside the Kalimantan land mask."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.services.wildfire_geometry import is_kalimantan_geometry

revision: str = "0021_filter_kalimantan_hotspots"
down_revision: str | None = "0020_merge_wildfire_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    events = sa.table(
        "disaster_events",
        sa.column("id", sa.Integer()),
        sa.column("slug", sa.String()),
    )
    hotspots = sa.table(
        "wildfire_hotspots",
        sa.column("id", sa.Integer()),
        sa.column("event_id", sa.Integer()),
        sa.column("geojson", sa.JSON()),
        sa.column("is_published", sa.Boolean()),
    )
    event_id = connection.execute(
        sa.select(events.c.id).where(events.c.slug == "kalimantan-2026")
    ).scalar_one_or_none()
    if event_id is None:
        return

    rows = connection.execute(
        sa.select(hotspots.c.id, hotspots.c.geojson).where(hotspots.c.event_id == event_id)
    ).mappings()
    out_of_scope = [
        row["id"]
        for row in rows
        if not isinstance(row["geojson"], dict) or not is_kalimantan_geometry(row["geojson"])
    ]
    if out_of_scope:
        connection.execute(
            sa.update(hotspots)
            .where(hotspots.c.id.in_(out_of_scope))
            .values(is_published=False)
        )


def downgrade() -> None:
    # Keep the cleanup migration one-way; records remain available for manual
    # recovery if the boundary source is revised later.
    pass
