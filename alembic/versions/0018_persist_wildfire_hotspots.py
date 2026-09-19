"""Persist normalized wildfire hotspot observations.

Revision ID: 0018_persist_wildfire_hotspots
Revises: 0017_seed_kalimantan_wildfire_event
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_persist_wildfire_hotspots"
down_revision: str | None = "0017_seed_kalimantan_wildfire_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "disaster_events",
        sa.Column("hotspot_last_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "wildfire_hotspots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("disaster_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=180), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("geojson", postgresql.JSONB(), nullable=False),
        sa.Column("stats", postgresql.JSONB(), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "external_id", name="uq_wildfire_hotspots_event_external"),
    )
    op.create_index("ix_wildfire_hotspots_event_id", "wildfire_hotspots", ["event_id"])
    op.create_index("ix_wildfire_hotspots_source", "wildfire_hotspots", ["source"])
    op.create_index("ix_wildfire_hotspots_acquired_at", "wildfire_hotspots", ["acquired_at"])
    op.create_index("ix_wildfire_hotspots_is_published", "wildfire_hotspots", ["is_published"])


def downgrade() -> None:
    op.drop_index("ix_wildfire_hotspots_is_published", table_name="wildfire_hotspots")
    op.drop_index("ix_wildfire_hotspots_acquired_at", table_name="wildfire_hotspots")
    op.drop_index("ix_wildfire_hotspots_source", table_name="wildfire_hotspots")
    op.drop_index("ix_wildfire_hotspots_event_id", table_name="wildfire_hotspots")
    op.drop_table("wildfire_hotspots")
    op.drop_column("disaster_events", "hotspot_last_synced_at")
