"""Add auditable PT Dahana plot-level carbon calibration records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_carbon_calibration"
down_revision: str | None = "0012_geospatial_expert_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table("carbon_calibration_datasets", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dataset_id", sa.String(128), nullable=False, unique=True), sa.Column("name", sa.String(255), nullable=False), sa.Column("manifest", sa.JSON(), nullable=False), sa.Column("access_classification", sa.String(32), nullable=False, server_default="restricted"), sa.Column("status", sa.String(32), nullable=False, server_default="draft"), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("owner_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")), sa.Column("validation_report", sa.JSON()), sa.Column("provenance", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_carbon_calibration_datasets_dataset_id", "carbon_calibration_datasets", ["dataset_id"])
    op.create_index("ix_carbon_calibration_datasets_status", "carbon_calibration_datasets", ["status"])
    op.create_table("carbon_calibration_files", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dataset_pk", sa.Integer(), sa.ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), nullable=False), sa.Column("source_name", sa.String(255), nullable=False), sa.Column("stored_path", sa.Text(), nullable=False), sa.Column("media_type", sa.String(128)), sa.Column("size_bytes", sa.BigInteger(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("file_kind", sa.String(64), nullable=False, server_default="unknown"), sa.Column("metadata_json", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_carbon_calibration_files_dataset_pk", "carbon_calibration_files", ["dataset_pk"])
    op.create_index("ix_carbon_calibration_files_sha256", "carbon_calibration_files", ["sha256"])
    op.create_table("carbon_field_plots", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dataset_pk", sa.Integer(), sa.ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), nullable=False), sa.Column("plot_id", sa.String(32), nullable=False), sa.Column("geometry_original", sa.JSON()), sa.Column("geometry_normalized", sa.JSON()), sa.Column("crs", sa.String(64), nullable=False, server_default="EPSG:32748"), sa.Column("area_m2", sa.Float()), sa.Column("area_ha", sa.Float()), sa.Column("attributes", sa.JSON()), sa.Column("uncertainty", sa.JSON()), sa.Column("qa_status", sa.String(32), nullable=False, server_default="pending"), sa.Column("source_document", sa.String(255)), sa.Column("source_table", sa.String(255)), sa.Column("survey_date", sa.String(32)))
    op.create_index("ix_carbon_field_plots_dataset_pk", "carbon_field_plots", ["dataset_pk"])
    op.create_index("ix_carbon_field_plots_plot_id", "carbon_field_plots", ["plot_id"])
    op.create_table("carbon_field_trees", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("plot_pk", sa.Integer(), sa.ForeignKey("carbon_field_plots.id", ondelete="CASCADE"), nullable=False), sa.Column("tree_id", sa.String(64), nullable=False), sa.Column("species", sa.String(255)), sa.Column("circumference_cm", sa.Float()), sa.Column("dbh_cm", sa.Float()), sa.Column("agb_kg", sa.Float()), sa.Column("bgb_kg", sa.Float()), sa.Column("carbon_agb_kg", sa.Float()), sa.Column("carbon_bgb_kg", sa.Float()), sa.Column("formula_version", sa.String(64), nullable=False, server_default="dahana-2026-v1"), sa.Column("validation_status", sa.String(32), nullable=False, server_default="pending"))
    op.create_index("ix_carbon_field_trees_plot_pk", "carbon_field_trees", ["plot_pk"])
    op.create_table("carbon_calibration_runs", sa.Column("id", sa.String(64), primary_key=True), sa.Column("dataset_pk", sa.Integer(), sa.ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), nullable=False), sa.Column("status", sa.String(32), nullable=False, server_default="queued"), sa.Column("method", sa.String(64), nullable=False), sa.Column("target_pool", sa.String(64), nullable=False), sa.Column("parameters", sa.JSON(), nullable=False), sa.Column("metrics", sa.JSON()), sa.Column("artifact_path", sa.Text()), sa.Column("artifact_sha256", sa.String(64)), sa.Column("error", sa.JSON()), sa.Column("progress", sa.Integer(), nullable=False, server_default="0"), sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_carbon_calibration_runs_dataset_pk", "carbon_calibration_runs", ["dataset_pk"])
    op.create_index("ix_carbon_calibration_runs_status", "carbon_calibration_runs", ["status"])
    op.create_table("carbon_calibration_models", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("model_id", sa.String(128), nullable=False, unique=True), sa.Column("dataset_pk", sa.Integer(), sa.ForeignKey("carbon_calibration_datasets.id", ondelete="CASCADE"), nullable=False), sa.Column("run_id", sa.String(64), sa.ForeignKey("carbon_calibration_runs.id", ondelete="SET NULL")), sa.Column("model_name", sa.String(255), nullable=False), sa.Column("version", sa.String(64), nullable=False), sa.Column("target_pool", sa.String(64), nullable=False), sa.Column("target_unit", sa.String(64), nullable=False, server_default="Mg C/ha"), sa.Column("status", sa.String(32), nullable=False, server_default="experimental"), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("artifact_path", sa.Text(), nullable=False), sa.Column("artifact_sha256", sa.String(64), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("approved_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")), sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_carbon_calibration_models_model_id", "carbon_calibration_models", ["model_id"])
    op.create_index("ix_carbon_calibration_models_dataset_pk", "carbon_calibration_models", ["dataset_pk"])
    op.create_index("ix_carbon_calibration_models_status", "carbon_calibration_models", ["status"])
    for code, description in (("calibration.read", "View carbon calibration datasets and validation"), ("calibration.write", "Manage carbon calibration datasets and models")):
        bind.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.name IN ('admin', 'geospatial_expert') "
            "AND p.code IN ('calibration.read', 'calibration.write') "
            "ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("carbon_calibration_models", "carbon_calibration_runs", "carbon_field_trees", "carbon_field_plots", "carbon_calibration_files", "carbon_calibration_datasets"):
        op.drop_table(table)
    for code in ("calibration.read", "calibration.write"):
        bind.execute(sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code})
