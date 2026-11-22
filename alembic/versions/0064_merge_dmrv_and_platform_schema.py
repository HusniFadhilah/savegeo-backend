"""Merge the advanced dMRV and platform database migration lines."""

revision = "0064_merge_dmrv_platform_heads"
down_revision = ("0052_dmrv_advanced_review_tools", "0063_geospatial_catalog")
branch_labels = None
depends_on = None


def upgrade():
    # Both parent migration lines have completed their schema work.
    pass


def downgrade():
    # A merge revision owns no schema objects.
    pass
