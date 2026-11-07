"""Versioned project monitoring periods."""
from alembic import op
import sqlalchemy as sa

revision = "0042_dmrv_monitoring_periods"
down_revision = "0041_dmrv_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmrv_monitoring_periods",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("boundary_version_id", sa.String(36), sa.ForeignKey("dmrv_boundary_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "name", name="uq_dmrv_monitoring_period_name"),
        sa.CheckConstraint("end_date >= start_date", name="ck_dmrv_monitoring_period_dates"),
        sa.CheckConstraint("status IN ('planned', 'in_progress', 'completed')", name="ck_dmrv_monitoring_period_status"),
    )
    op.create_index("ix_dmrv_monitoring_periods_project_id", "dmrv_monitoring_periods", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_dmrv_monitoring_periods_project_id", table_name="dmrv_monitoring_periods")
    op.drop_table("dmrv_monitoring_periods")
