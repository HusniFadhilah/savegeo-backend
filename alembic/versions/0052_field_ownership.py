"""Preserve existing fields and isolate newly created boundaries by principal."""

from alembic import op
import sqlalchemy as sa

revision = "0052_field_ownership"
down_revision = "0051_dmrv_integrity_workspace"
branch_labels = None
depends_on = None


def upgrade():
    # Legacy ownership cannot be inferred safely. Keep those rows intact and
    # inaccessible through user APIs until an operator attributes them.
    op.add_column("fields", sa.Column("owner_key", sa.String(32), nullable=True))
    op.create_index("ix_fields_owner_key", "fields", ["owner_key"])


def downgrade():
    op.drop_index("ix_fields_owner_key", table_name="fields")
    op.drop_column("fields", "owner_key")
