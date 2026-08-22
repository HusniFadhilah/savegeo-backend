"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-30

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(128), unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_login", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_admin_users_username", "admin_users", ["username"])

    op.create_table(
        "gee_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("client_email", sa.String(256), nullable=False),
        sa.Column("json_filename", sa.String(256), nullable=False),
        sa.Column("bucket_path", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("admin_users.id")),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
    )

    op.create_table(
        "uploaded_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("model_type", sa.String(50), nullable=False),
        sa.Column("algorithm", sa.String(100)),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("filepath", sa.Text(), nullable=False),
        sa.Column("file_size_kb", sa.Float()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text()),
        sa.Column("version", sa.String(50)),
        sa.Column("metrics", postgresql.JSONB()),
        sa.Column("feature_names", postgresql.JSONB()),
        sa.Column("metadata_json", postgresql.JSONB()),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("admin_users.id")),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_uploaded_models_name", "uploaded_models", ["name"])

    op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False, unique=True),
        sa.Column("value", sa.Text()),
        sa.Column("value_type", sa.String(16), nullable=False, server_default="string"),
        sa.Column("category", sa.String(64), nullable=False, server_default="general"),
        sa.Column("label", sa.String(256)),
        sa.Column("description", sa.Text()),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("admin_users.id")),
    )
    op.create_index("ix_system_config_key", "system_config", ["key"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False, server_default="Percakapan Baru"),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("has_image", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("file_name", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "company_boundaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("company_name", sa.String(255)),
        sa.Column("industry_type", sa.String(50), nullable=False),
        sa.Column("sub_type", sa.String(100)),
        sa.Column("province", sa.String(100)),
        sa.Column("district", sa.String(100)),
        sa.Column("geojson", postgresql.JSONB(), nullable=False),
        sa.Column("area_ha", sa.Float()),
        sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
        sa.Column("source_url", sa.String(500)),
        sa.Column("description", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("company_boundaries")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_index("ix_system_config_key", table_name="system_config")
    op.drop_table("system_config")
    op.drop_index("ix_uploaded_models_name", table_name="uploaded_models")
    op.drop_table("uploaded_models")
    op.drop_table("gee_credentials")
    op.drop_index("ix_admin_users_username", table_name="admin_users")
    op.drop_table("admin_users")
