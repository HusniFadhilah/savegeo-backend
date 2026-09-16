"""Add standalone wildfire event metadata and public publication flags.

Revision ID: 0015_wildfire_event_metadata
Revises: 0014_carbon_scale_10m
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_wildfire_event_metadata"
down_revision: str | None = "0014_carbon_scale_10m"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = [
        sa.Column("slug", sa.String(180)), sa.Column("short_title", sa.String(120)),
        sa.Column("monitoring_from", sa.Date()), sa.Column("monitoring_to", sa.Date()),
        sa.Column("published_at", sa.DateTime(timezone=True)), sa.Column("last_data_at", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)), sa.Column("year", sa.Integer()),
        sa.Column("country_code", sa.String(3), server_default="ID"),
        sa.Column("province_codes", postgresql.JSONB()), sa.Column("city_codes", postgresql.JSONB()),
        sa.Column("bbox", postgresql.JSONB()), sa.Column("center_lat", sa.Float()), sa.Column("center_lon", sa.Float()),
        sa.Column("default_zoom", sa.Integer()), sa.Column("source_ids", postgresql.JSONB()),
        sa.Column("hotspot_dataset_ids", postgresql.JSONB()), sa.Column("burned_area_dataset_ids", postgresql.JSONB()),
        sa.Column("boundary_source", sa.String(255)), sa.Column("methodology", sa.Text()),
        sa.Column("limitations", postgresql.JSONB()), sa.Column("is_featured", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
    ]
    for column in columns:
        op.add_column("disaster_events", column)
    op.create_index("ix_disaster_events_slug", "disaster_events", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_disaster_events_slug", table_name="disaster_events")
    for name in ("updated_by", "is_public", "is_featured", "limitations", "methodology", "boundary_source", "burned_area_dataset_ids", "hotspot_dataset_ids", "source_ids", "default_zoom", "center_lon", "center_lat", "bbox", "city_codes", "province_codes", "country_code", "year", "last_synced_at", "last_data_at", "published_at", "monitoring_to", "monitoring_from", "short_title", "slug"):
        op.drop_column("disaster_events", name)
