"""Durable project contexts and transactional run quota."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0048_workspace_analysis"
down_revision = "0047_organizations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_dmrv_project_organization", "dmrv_projects", ["id", "organization_id"])
    op.create_unique_constraint("uq_dmrv_job_project", "dmrv_jobs", ["id", "project_id"])
    op.create_unique_constraint("uq_dmrv_period_project", "dmrv_monitoring_periods", ["id", "project_id"])
    op.create_unique_constraint("uq_dmrv_boundary_project", "dmrv_boundary_versions", ["id", "project_id"])
    op.create_table("workspace_analysis_runs",
        sa.Column("job_id", sa.String(36), sa.ForeignKey("dmrv_jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period_id", sa.String(36), sa.ForeignKey("dmrv_monitoring_periods.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("boundary_id", sa.String(36), sa.ForeignKey("dmrv_boundary_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("context", JSONB(), nullable=False), sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id", "organization_id"], ["dmrv_projects.id", "dmrv_projects.organization_id"], name="fk_run_project_organization"),
        sa.ForeignKeyConstraint(["job_id", "project_id"], ["dmrv_jobs.id", "dmrv_jobs.project_id"], name="fk_run_job_project"),
        sa.ForeignKeyConstraint(["period_id", "project_id"], ["dmrv_monitoring_periods.id", "dmrv_monitoring_periods.project_id"], name="fk_run_period_project"),
        sa.ForeignKeyConstraint(["boundary_id", "project_id"], ["dmrv_boundary_versions.id", "dmrv_boundary_versions.project_id"], name="fk_run_boundary_project"))
    op.create_index("ix_workspace_analysis_runs_organization_id", "workspace_analysis_runs", ["organization_id"])
    op.create_index("ix_workspace_analysis_runs_project_id", "workspace_analysis_runs", ["project_id"])
    op.create_table("workspace_quotas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("cycle_start", sa.Date(), nullable=False), sa.Column("run_limit", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("policy_version", sa.String(32), nullable=False, server_default="analysis_run_v1"),
        sa.UniqueConstraint("organization_id", "cycle_start", name="uq_workspace_quota_cycle"),
        sa.CheckConstraint("run_limit >= 0 AND reserved >= 0 AND used >= 0 AND reserved + used <= run_limit", name="ck_workspace_quota_balance"))
    op.create_index("ix_workspace_quotas_organization_id", "workspace_quotas", ["organization_id"])
    op.create_table("workspace_usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("quota_id", sa.String(36), sa.ForeignKey("workspace_quotas.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("dmrv_jobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event", sa.String(16), nullable=False), sa.Column("analysis_kind", sa.String(64), nullable=False),
        sa.Column("area_ha", sa.Float(), nullable=False), sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("detail", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "event", name="uq_workspace_usage_event"),
        sa.CheckConstraint("event IN ('reserve', 'consume', 'release')", name="ck_workspace_usage_event"))
    op.create_index("ix_workspace_usage_events_quota_id", "workspace_usage_events", ["quota_id"])
    op.create_index("ix_workspace_usage_events_job_id", "workspace_usage_events", ["job_id"])


def downgrade():
    for table in ("workspace_usage_events", "workspace_quotas", "workspace_analysis_runs"):
        op.drop_table(table)
    for table, constraint in (("dmrv_boundary_versions", "uq_dmrv_boundary_project"), ("dmrv_monitoring_periods", "uq_dmrv_period_project"),
                              ("dmrv_jobs", "uq_dmrv_job_project"), ("dmrv_projects", "uq_dmrv_project_organization")):
        op.drop_constraint(constraint, table, type_="unique")
