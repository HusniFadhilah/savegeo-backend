"""Join the geospatial catalog and restricted-runtime RLS migration heads."""

revision = "0064_merge_0063_heads"
down_revision = ("0063_geospatial_catalog", "0063_dmrv_runtime_rls")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    # The parent migrations own their respective schema changes. This revision
    # exists only to make the combined migration graph have one unambiguous head.
    pass
