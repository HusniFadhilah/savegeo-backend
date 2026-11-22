"""Bind model artifacts to database identity and index bounded list queries."""
from alembic import op
import sqlalchemy as sa

revision = "0057_model_identity_and_indexes"
down_revision = "0056_explicit_access_flags"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("uploaded_models", sa.Column("artifact_sha256", sa.String(64)))
    op.execute("UPDATE uploaded_models SET artifact_sha256 = metadata_json->>'artifact_sha256' WHERE (metadata_json->>'artifact_sha256') ~ '^[0-9a-f]{64}$'")
    inspector = sa.inspect(op.get_bind())
    for fk in inspector.get_foreign_keys("uploaded_models"):
        if fk["constrained_columns"] == ["uploaded_by"] and fk.get("name"):
            op.drop_constraint(fk["name"], "uploaded_models", type_="foreignkey")
            op.create_foreign_key(fk["name"], "uploaded_models", "admin_users", ["uploaded_by"], ["id"], ondelete="SET NULL")
    op.drop_index("ix_fields_owner_key", table_name="fields")
    for name, table, columns in (
        ("ix_companies_active_name_id", "company_boundaries", ["is_active", "name", "id"]),
        ("ix_models_active_type_id", "uploaded_models", ["is_active", "model_type", "id"]),
        ("ix_fields_owner_created_id", "fields", ["owner_key", "created_at", "id"]),
        ("ix_provider_cache_provider_expiry", "provider_cache", ["provider", "expires_at"]),
    ):
        op.create_index(name, table, columns)


def downgrade():
    op.create_index("ix_fields_owner_key", "fields", ["owner_key"])
    for name, table in (("ix_companies_active_name_id", "company_boundaries"),
        ("ix_models_active_type_id", "uploaded_models"), ("ix_fields_owner_created_id", "fields"),
        ("ix_provider_cache_provider_expiry", "provider_cache")):
        op.drop_index(name, table_name=table)
    inspector = sa.inspect(op.get_bind())
    for fk in inspector.get_foreign_keys("uploaded_models"):
        if fk["constrained_columns"] == ["uploaded_by"] and fk.get("name"):
            op.drop_constraint(fk["name"], "uploaded_models", type_="foreignkey")
            op.create_foreign_key(fk["name"], "uploaded_models", "admin_users", ["uploaded_by"], ["id"])
    op.drop_column("uploaded_models", "artifact_sha256")
