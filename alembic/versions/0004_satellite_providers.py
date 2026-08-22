"""satellite_providers admin-override table

Same overlay pattern as `datasets` (migration 0002): the static registry
(app/registries/satellite_provider_registry.py) stays the source of truth
for GEE collection ids / band maps, this table only holds admin-editable
display metadata + an is_active toggle.

Revision ID: 0004_satellite_providers
Revises: 0003_audit_log_fk_set_null
Create Date: 2026-08-17

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_satellite_providers"
down_revision: str | None = "0003_audit_log_fk_set_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "satellite_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255)),
        sa.Column("provider", sa.String(255)),
        sa.Column("resolution_label", sa.String(128)),
        sa.Column("description", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_satellite_providers_key", "satellite_providers", ["key"])


def downgrade() -> None:
    op.drop_index("ix_satellite_providers_key", table_name="satellite_providers")
    op.drop_table("satellite_providers")
