"""Explicit customer workspaces; legacy projects retain their existing scope.

No access is inferred from the old organization display-name column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0047_organizations"
down_revision = "0046_green_ranking_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("personal_owner", sa.String(32), unique=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("organization_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("principal_type", sa.String(8), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "principal_type", "principal_id", name="uq_org_member_principal"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_org_member_role"),
        sa.CheckConstraint("principal_type IN ('user', 'admin')", name="ck_org_member_principal_type"))
    op.create_index("ix_organization_members_organization_id", "organization_members", ["organization_id"])
    op.create_index("ix_org_members_principal_active", "organization_members", ["principal_type", "principal_id", "is_active"])
    op.create_table("organization_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_organization_invitations_organization_id", "organization_invitations", ["organization_id"])
    op.create_table("workspace_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("principal_key", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("principal_key", "key", name="uq_workspace_request_key"))
    op.add_column("dmrv_projects", sa.Column("organization_id", sa.String(36)))
    op.create_foreign_key("fk_dmrv_project_organization", "dmrv_projects", "organizations", ["organization_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_dmrv_projects_organization_id", "dmrv_projects", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_dmrv_projects_organization_id", table_name="dmrv_projects")
    op.drop_constraint("fk_dmrv_project_organization", "dmrv_projects", type_="foreignkey")
    op.drop_column("dmrv_projects", "organization_id")
    for name in ("workspace_requests", "organization_invitations", "organization_members", "organizations"):
        op.drop_table(name)
