"""dMRV field data: plots, visits, tree measurements, import batches."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0040_dmrv_field_data"
down_revision = "0039_dmrv_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmrv_import_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dmrv_import_batches_project_id", "dmrv_import_batches", ["project_id"])
    op.create_table(
        "dmrv_survey_plots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plot_code", sa.String(64), nullable=False),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("area_m2", sa.Float()),
        sa.Column("area_basis", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "plot_code", name="uq_dmrv_plot_code"),
    )
    op.create_index("ix_dmrv_survey_plots_project_id", "dmrv_survey_plots", ["project_id"])
    op.create_table(
        "dmrv_survey_visits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plot_id", sa.String(36), sa.ForeignKey("dmrv_survey_plots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visit_date", sa.Date(), nullable=False),
        sa.Column("date_precision", sa.String(8), nullable=False),
        sa.Column("operator", sa.String(128)),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("dmrv_import_batches.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("plot_id", "visit_date", name="uq_dmrv_visit_plot_date"),
    )
    op.create_index("ix_dmrv_survey_visits_plot_id", "dmrv_survey_visits", ["plot_id"])
    op.create_table(
        "dmrv_tree_measurements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("visit_id", sa.String(36), sa.ForeignKey("dmrv_survey_visits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tree_code", sa.String(64), nullable=False),
        sa.Column("species", sa.String(255)),
        sa.Column("circumference_cm", sa.Float()),
        sa.Column("dbh_cm", sa.Float()),
        sa.Column("height_m", sa.Float()),
        sa.Column("biomass_kg", sa.Float()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("biomass_source", sa.String(16)),
        sa.Column("qa_flags", postgresql.JSONB(), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("dmrv_import_batches.id", ondelete="SET NULL")),
        sa.UniqueConstraint("visit_id", "tree_code", name="uq_dmrv_tree_visit_code"),
    )
    op.create_index("ix_dmrv_tree_measurements_visit_id", "dmrv_tree_measurements", ["visit_id"])


def downgrade() -> None:
    op.drop_index("ix_dmrv_tree_measurements_visit_id", table_name="dmrv_tree_measurements")
    op.drop_table("dmrv_tree_measurements")
    op.drop_index("ix_dmrv_survey_visits_plot_id", table_name="dmrv_survey_visits")
    op.drop_table("dmrv_survey_visits")
    op.drop_index("ix_dmrv_survey_plots_project_id", table_name="dmrv_survey_plots")
    op.drop_table("dmrv_survey_plots")
    op.drop_index("ix_dmrv_import_batches_project_id", table_name="dmrv_import_batches")
    op.drop_table("dmrv_import_batches")
