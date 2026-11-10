"""Pool-specific field observations; preserves legacy calibration data and users."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0045_carbon_field_observations"
down_revision = "0044_dmrv_submissions_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "carbon_field_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "dataset_pk",
            sa.Integer(),
            sa.ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("pool", sa.String(8), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("observed_mg_c_ha", sa.Float(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("predictions", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("dataset_pk", "identity_key", name="uq_carbon_field_identity"),
    )
    op.create_index("ix_carbon_field_observations_dataset_pk", "carbon_field_observations", ["dataset_pk"])


def downgrade():
    op.drop_table("carbon_field_observations")
