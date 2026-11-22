"""Join the platform and dMRV migration branches and secure new runtime tables."""

from alembic import op


revision = "0065_merge_all_schema_heads"
down_revision = ("0064_merge_dmrv_platform_heads", "0064_merge_0063_heads")
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in ("dmrv_credit_events", "dmrv_field_captures"):
        op.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' "
            f"AND tablename = '{table}' AND policyname = 'savegeo_runtime_access') THEN "
            f"EXECUTE 'CREATE POLICY savegeo_runtime_access ON public.{table} FOR ALL TO PUBLIC "
            "USING (current_user = ''savegeo_runtime'') "
            "WITH CHECK (current_user = ''savegeo_runtime'')'; END IF; END $$"
        )


def downgrade():
    # Keep runtime access policies on the additive dMRV tables when moving back
    # to either parent head; the table-creation migration owns their lifecycle.
    pass
