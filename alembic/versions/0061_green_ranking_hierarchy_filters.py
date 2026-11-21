"""Index hierarchy codes for reusable national green-ranking snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "0061_green_ranking_hierarchy_filters"
down_revision = "0060_field_pred_queue"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("province", "city", "district"):
        op.add_column(
            "green_ranking_snapshot_rows",
            sa.Column(column, sa.String(length=64), nullable=True),
        )
    op.execute(
        sa.text(
            """
            UPDATE green_ranking_snapshot_rows
            SET province = payload ->> 'province',
                city = payload ->> 'city',
                district = payload ->> 'district'
            """
        )
    )
    for column in ("province", "city", "district"):
        op.create_index(
            f"ix_green_ranking_snapshot_{column}",
            "green_ranking_snapshot_rows",
            ["scope_key", column, "eligible", "rank"],
        )


def downgrade():
    for column in ("province", "city", "district"):
        op.drop_index(
            f"ix_green_ranking_snapshot_{column}",
            table_name="green_ranking_snapshot_rows",
        )
        op.drop_column("green_ranking_snapshot_rows", column)
