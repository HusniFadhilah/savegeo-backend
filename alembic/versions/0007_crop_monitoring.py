"""Crop Monitoring schema

Adds: fields (named farm/field boundary, shared/no-owner - Crop Monitoring is
a Sidebar tab like Carbon/LC-Change, not a login-gated feature, so this table
has no FK to `users`). Growth-stage/commodity metadata itself lives in
`app.registries.crop_registry` (static), not a DB table. Crop-risk-score
weights are added as `system_config` rows via `default_configs.py`, not a
migration (that table already exists).

Revision ID: 0007_crop_monitoring
Revises: 0006_disaster_intelligence
Create Date: 2026-08-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_crop_monitoring"
down_revision: Union[str, None] = "0006_disaster_intelligence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("geojson", postgresql.JSONB(), nullable=False),
        sa.Column("area_ha", sa.Float(), nullable=False),
        sa.Column("commodity", sa.String(50), nullable=False),
        sa.Column("variety", sa.String(100)),
        sa.Column("planting_date", sa.Date()),
        sa.Column("season_label", sa.String(50)),
        sa.Column("estimated_harvest_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_fields_commodity", "fields", ["commodity"])


def downgrade() -> None:
    op.drop_table("fields")
