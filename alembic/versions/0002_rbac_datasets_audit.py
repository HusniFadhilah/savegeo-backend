"""roles/permissions/audit_logs/datasets catalog + admin_users.role_id

Revision ID: 0002_rbac_datasets_audit
Revises: 0001_initial_schema
Create Date: 2026-08-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_rbac_datasets_audit"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.Text()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.Text()),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_id", sa.Integer(), sa.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
    )

    op.add_column("admin_users", sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=True))

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admin_user_id", sa.Integer(), sa.ForeignKey("admin_users.id")),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False, unique=True),
        sa.Column("module", sa.String(32), nullable=False),
        sa.Column("provider_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("full_name", sa.String(255)),
        sa.Column("description", sa.Text()),
        sa.Column("attribution", sa.Text()),
        sa.Column("limitations", postgresql.JSONB()),
        sa.Column("unit", sa.String(64)),
        sa.Column("resolution", sa.String(32)),
        sa.Column("year_min", sa.Integer()),
        sa.Column("year_max", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extra_metadata", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_datasets_key", "datasets", ["key"])


def downgrade() -> None:
    op.drop_index("ix_datasets_key", table_name="datasets")
    op.drop_table("datasets")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_column("admin_users", "role_id")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
