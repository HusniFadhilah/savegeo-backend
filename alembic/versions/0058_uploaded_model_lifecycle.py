"""Add an auditable upload, evaluation, review and activation lifecycle."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0058_uploaded_model_lifecycle"
down_revision = "0057_model_identity_and_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("uploaded_models", sa.Column("lifecycle_state", sa.String(32), nullable=False, server_default="experimental"))
    op.add_column("uploaded_models", sa.Column("approved_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")))
    op.add_column("uploaded_models", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column("uploaded_models", sa.Column("approval_digest", sa.String(64)))
    # Old active/default flags did not carry an independent evaluation and review record.
    # Quarantine those rows for explicit re-evaluation instead of inventing approvals.
    op.execute("UPDATE uploaded_models SET lifecycle_state='experimental', is_active=false, is_default=false WHERE is_active=true OR is_default=true")
    op.create_check_constraint("ck_uploaded_models_lifecycle_state", "uploaded_models",
        "lifecycle_state IN ('experimental','training','evaluated','review_pending','approved','active','rejected','archived')")
    op.create_check_constraint("ck_uploaded_models_lifecycle_active", "uploaded_models",
        "(lifecycle_state = 'active') = is_active")
    op.create_check_constraint("ck_uploaded_models_default_active", "uploaded_models",
        "NOT is_default OR (is_active AND lifecycle_state = 'active')")
    op.create_check_constraint("ck_uploaded_models_approval_attestation", "uploaded_models",
        "lifecycle_state NOT IN ('approved','active') OR (approved_by IS NOT NULL AND approved_by IS DISTINCT FROM uploaded_by AND approval_digest IS NOT NULL)")
    op.create_index("uq_uploaded_models_default_type", "uploaded_models", ["model_type"], unique=True,
                    postgresql_where=sa.text("is_default IS TRUE"))

    op.create_table("uploaded_model_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("uploaded_models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_name", sa.String(255), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_source", sa.Text()),
        sa.Column("dataset_storage_path", sa.Text()),
        sa.Column("manifest", JSONB(), nullable=False, server_default="{}"),
        sa.Column("metrics", JSONB(), nullable=False, server_default="{}"),
        sa.Column("evaluated_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_model_evaluations_model_created", "uploaded_model_evaluations", ["model_id", "created_at"])
    op.create_table("uploaded_model_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("uploaded_models.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evaluation_id", sa.Integer(), sa.ForeignKey("uploaded_model_evaluations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('approved','rejected')", name="ck_uploaded_model_reviews_decision"),
    )
    op.create_index("ix_model_reviews_model_created", "uploaded_model_reviews", ["model_id", "created_at"])

    bind = op.get_bind()
    bind.execute(sa.text("INSERT INTO permissions (code, description) VALUES ('model.approve', 'Independently review and approve executable models') ON CONFLICT (code) DO UPDATE SET description=EXCLUDED.description"))
    bind.execute(sa.text("INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r CROSS JOIN permissions p WHERE r.name='admin' AND p.code='model.approve' ON CONFLICT DO NOTHING"))


def downgrade():
    op.drop_index("uq_uploaded_models_default_type", table_name="uploaded_models")
    op.drop_index("ix_model_reviews_model_created", table_name="uploaded_model_reviews")
    op.drop_table("uploaded_model_reviews")
    op.drop_index("ix_model_evaluations_model_created", table_name="uploaded_model_evaluations")
    op.drop_table("uploaded_model_evaluations")
    op.drop_constraint("ck_uploaded_models_approval_attestation", "uploaded_models", type_="check")
    op.drop_constraint("ck_uploaded_models_default_active", "uploaded_models", type_="check")
    op.drop_constraint("ck_uploaded_models_lifecycle_active", "uploaded_models", type_="check")
    op.drop_constraint("ck_uploaded_models_lifecycle_state", "uploaded_models", type_="check")
    op.drop_column("uploaded_models", "approval_digest")
    op.drop_column("uploaded_models", "approved_at")
    op.drop_column("uploaded_models", "approved_by")
    op.drop_column("uploaded_models", "lifecycle_state")
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_id=(SELECT id FROM permissions WHERE code='model.approve')"))
    bind.execute(sa.text("DELETE FROM permissions WHERE code='model.approve'"))
