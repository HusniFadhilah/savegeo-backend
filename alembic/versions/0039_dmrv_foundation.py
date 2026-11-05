"""dMRV foundation: projects, membership, boundary versions, audit, idempotency."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0039_dmrv_foundation"
down_revision = "0038_ilham_full_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dmrv_projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("organization", sa.String(255)),
        sa.Column("activity_type", sa.String(32), nullable=False),
        sa.Column("jurisdiction", sa.String(128)),
        sa.Column("methodology_id", sa.String(64)),
        sa.Column("methodology_version", sa.String(32)),
        sa.Column("start_date", sa.Date()),
        sa.Column("crediting_period_years", sa.Integer()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "dmrv_project_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_type", sa.String(8), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("added_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "principal_type", "principal_id", name="uq_dmrv_member_principal"),
    )
    op.create_index("ix_dmrv_project_members_project_id", "dmrv_project_members", ["project_id"])
    op.create_table(
        "dmrv_boundary_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("original_geojson", postgresql.JSONB(), nullable=False),
        sa.Column("validated_geojson", postgresql.JSONB(), nullable=False),
        sa.Column("crs", sa.String(32), nullable=False),
        sa.Column("area_ha", sa.Float(), nullable=False),
        sa.Column("area_method", sa.String(64), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "version", name="uq_dmrv_boundary_version"),
    )
    op.create_index("ix_dmrv_boundary_versions_project_id", "dmrv_boundary_versions", ["project_id"])
    op.create_table(
        "dmrv_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(36)),
        sa.Column("actor_type", sa.String(8), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64)),
        sa.Column("before", postgresql.JSONB()),
        sa.Column("after", postgresql.JSONB()),
        sa.Column("reason", sa.Text()),
        sa.Column("request_id", sa.String(64)),
        sa.Column("snapshot_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dmrv_audit_project_created", "dmrv_audit_events", ["project_id", "created_at"])
    op.create_table(
        "dmrv_idempotency_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("principal_key", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("principal_key", "key", name="uq_dmrv_idempotency_principal_key"),
    )


def downgrade() -> None:
    op.drop_table("dmrv_idempotency_keys")
    op.drop_index("ix_dmrv_audit_project_created", table_name="dmrv_audit_events")
    op.drop_table("dmrv_audit_events")
    op.drop_index("ix_dmrv_boundary_versions_project_id", table_name="dmrv_boundary_versions")
    op.drop_table("dmrv_boundary_versions")
    op.drop_index("ix_dmrv_project_members_project_id", table_name="dmrv_project_members")
    op.drop_table("dmrv_project_members")
    op.drop_table("dmrv_projects")
