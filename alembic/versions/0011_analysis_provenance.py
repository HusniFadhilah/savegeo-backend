"""Persist reproducibility metadata for asynchronous analyses."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_analysis_provenance"
down_revision: str | None = "0010_carbon_scale_100m"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_provenance",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("analysis_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("dataset_id", sa.String(length=255)),
        sa.Column("dataset_version", sa.String(length=128)),
        sa.Column("acquisition_date", sa.String(length=64)),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("aoi_checksum", sa.String(length=64)),
        sa.Column("crs", sa.String(length=128)),
        sa.Column("resolution", sa.Float()),
        sa.Column("cloud_mask", sa.JSON()),
        sa.Column("model_id", sa.String(length=255)),
        sa.Column("model_version", sa.String(length=128)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("quality_flags", sa.JSON()),
        sa.Column("confidence", sa.JSON()),
        sa.Column("artifact_checksums", sa.JSON()),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_analysis_provenance_analysis_type", "analysis_provenance", ["analysis_type"])
    op.create_index("ix_analysis_provenance_status", "analysis_provenance", ["status"])


def downgrade() -> None:
    op.drop_index("ix_analysis_provenance_status", table_name="analysis_provenance")
    op.drop_index("ix_analysis_provenance_analysis_type", table_name="analysis_provenance")
    op.drop_table("analysis_provenance")
