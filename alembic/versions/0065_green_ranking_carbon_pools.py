"""Persist carbon pool densities with green ranking snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "0065_green_rank_carbon"
# Databases created from the previous integrated migration branch record
# 0064_merge_dmrv_platform_heads. Merge that history with the current split
# RLS/catalogue heads so both existing databases and fresh installs advance
# through the same carbon-pool migration.
down_revision = ("0064_merge_0063_heads", "0064_merge_dmrv_platform_heads")
branch_labels = None
depends_on = None


def upgrade():
    for column in ("agb_mg_c_ha", "bgb_mg_c_ha", "soc_mg_c_ha", "litter_mg_c_ha", "deadwood_mg_c_ha"):
        op.add_column("green_ranking_snapshot_rows", sa.Column(column, sa.Float(), nullable=True))


def downgrade():
    for column in ("deadwood_mg_c_ha", "litter_mg_c_ha", "soc_mg_c_ha", "bgb_mg_c_ha", "agb_mg_c_ha"):
        op.drop_column("green_ranking_snapshot_rows", column)
