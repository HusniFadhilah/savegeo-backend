"""Persist the cloud geospatial dataset catalog across API workers."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0063_geospatial_catalog"
down_revision = "0062_colorblind_carbon"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "geospatial_datasets",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("document", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("geospatial_datasets")
