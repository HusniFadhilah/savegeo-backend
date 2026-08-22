"""Local raster imagery support for SatelliteImagery

Adds `source_kind` ("gee" | "local_upload", default "gee" so every existing
row keeps its current meaning) and `local_file_path` (path relative to
`settings.upload_dir`, only set when source_kind="local_upload") to
`satellite_imagery`. Needed to serve admin-ingested high-resolution
GeoTIFFs (e.g. BlackSky tasking imagery) as XYZ tiles via a new local
rasterio-based tile endpoint, alongside the existing 100%-GEE-based
`preview_tile_url` pattern - see
`backend/docs/banjir-sumatera-2025-ingestion-prompt.md`.

Revision ID: 0008_local_imagery
Revises: 0007_crop_monitoring
Create Date: 2026-08-22

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_local_imagery"
down_revision: str | None = "0007_crop_monitoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "satellite_imagery",
        sa.Column("source_kind", sa.String(20), nullable=False, server_default="gee"),
    )
    op.add_column(
        "satellite_imagery",
        sa.Column("local_file_path", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("satellite_imagery", "local_file_path")
    op.drop_column("satellite_imagery", "source_kind")
