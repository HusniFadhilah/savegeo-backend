"""Join carbon-ranking and platform/dMRV migration lines."""

revision = "0066_merge_green_carbon"
down_revision = ("0065_green_rank_carbon", "0065_merge_all_schema_heads")
branch_labels = None
depends_on = None


def upgrade():
    # The carbon-pool schema change is owned by 0065_green_rank_carbon.
    pass


def downgrade():
    pass
