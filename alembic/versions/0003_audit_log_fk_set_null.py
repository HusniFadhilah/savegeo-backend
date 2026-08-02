"""audit_logs.admin_user_id FK -> ON DELETE SET NULL

An admin account must be deletable even after it has audit history - the
original FK (implicit NO ACTION/RESTRICT) blocked deleting any admin who had
ever triggered a logged mutation, which is exactly what the new
DELETE /api/admin/users/{id} endpoint needs to do. Found via live testing.

Revision ID: 0003_audit_log_fk_set_null
Revises: 0002_rbac_datasets_audit
Create Date: 2026-08-01

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_audit_log_fk_set_null"
down_revision: Union[str, None] = "0002_rbac_datasets_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("audit_logs_admin_user_id_fkey", "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        "audit_logs_admin_user_id_fkey",
        "audit_logs",
        "admin_users",
        ["admin_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("audit_logs_admin_user_id_fkey", "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        "audit_logs_admin_user_id_fkey",
        "audit_logs",
        "admin_users",
        ["admin_user_id"],
        ["id"],
    )
