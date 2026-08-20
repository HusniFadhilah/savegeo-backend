"""satellite_providers: add technical/spec override columns

Extends the overlay table from migration 0004 (display-only fields) to also
allow overriding the GEE collection id and the generic-role->native-band map
per provider, plus the remaining spec fields (resolution/revisit/swath/
launch/bands_available) - so an admin can point "sentinel2" at a different
(but band-compatible) GEE asset, or register a brand-new provider entirely
via DB rows alone, without a code change. All nullable; a null column still
falls back to the static registry default (see satellite_provider_repo.py).

Revision ID: 0005_satellite_tech_fields
Revises: 0004_satellite_providers
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_satellite_tech_fields"
down_revision: Union[str, None] = "0004_satellite_providers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("satellite_providers", sa.Column("gee_collection", sa.String(255)))
    op.add_column("satellite_providers", sa.Column("band_role_map", postgresql.JSONB()))
    op.add_column("satellite_providers", sa.Column("resolution_m", sa.Integer()))
    op.add_column("satellite_providers", sa.Column("revisit_days", sa.Integer()))
    op.add_column("satellite_providers", sa.Column("swath_km", sa.Integer()))
    op.add_column("satellite_providers", sa.Column("launch", sa.String(64)))
    op.add_column("satellite_providers", sa.Column("start_year", sa.Integer()))
    op.add_column("satellite_providers", sa.Column("bands_available", postgresql.JSONB()))


def downgrade() -> None:
    for col in ("bands_available", "start_year", "launch", "swath_km", "revisit_days", "resolution_m", "band_role_map", "gee_collection"):
        op.drop_column("satellite_providers", col)
