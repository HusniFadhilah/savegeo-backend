"""Refresh untouched carbon-pool palettes with color-vision-accessible ramps."""

from alembic import op
import sqlalchemy as sa

revision = "0062_colorblind_carbon"
down_revision = "0061_green_rank_hierarchy"
branch_labels = None
depends_on = None


PALETTES = {
    "carbon.pool.agb.vis_palette": (
        "ffffcc,c2e699,78c679,31a354,006837",
        "440154,414487,2a788e,22a884,7ad151,fde725",
    ),
    "carbon.pool.bgb.vis_palette": (
        "eff3ff,bdd7e7,6baed6,3182bd,08519c",
        "00204c,414f79,697777,97935f,c5b54f,ffea46",
    ),
    "carbon.pool.soc.vis_palette": (
        "f2f0f7,cbc9e2,9e9ac8,756bb1,54278f",
        "0d0887,5c01a6,9c179e,cc4778,ed7953,f0f921",
    ),
    "carbon.pool.litter.vis_palette": (
        "fff7bc,fec44f,fe9929,ec7014,cc4c02",
        "000004,420a68,932667,dd513a,fba40a,fcffa4",
    ),
    "carbon.pool.deadwood.vis_palette": (
        "f7f4f0,d8c3a5,a67c52,795548,3e2723",
        "000004,3b0f70,8c2981,de4968,fe9f6d,fcfdbf",
    ),
}


def upgrade():
    bind = op.get_bind()
    statement = sa.text(
        "UPDATE system_config SET value = :new_value "
        "WHERE key = :key AND value = :old_value"
    )
    for key, (old_value, new_value) in PALETTES.items():
        bind.execute(statement, {"key": key, "old_value": old_value, "new_value": new_value})


def downgrade():
    bind = op.get_bind()
    statement = sa.text(
        "UPDATE system_config SET value = :old_value "
        "WHERE key = :key AND value = :new_value"
    )
    for key, (old_value, new_value) in PALETTES.items():
        bind.execute(statement, {"key": key, "old_value": old_value, "new_value": new_value})
