"""Allow the restricted SaveGeo runtime role through dMRV table RLS."""

from alembic import op


revision = "0063_dmrv_runtime_rls"
down_revision = "0062_colorblind_carbon"
branch_labels = None
depends_on = None


TABLES = (
    "dmrv_integrity_records",
    "dmrv_integrity_revisions",
    "dmrv_integrity_documents",
    "dmrv_risk_policies",
)


def upgrade():
    # The runtime role has table grants but deliberately has no BYPASSRLS and
    # is not the table owner. Gate its RLS access by exact database identity;
    # API code remains responsible for project membership and action checks.
    # PUBLIC is the policy target only; the expression denies every other
    # database role even if it later receives table privileges.
    for table in TABLES:
        op.execute(
            f"CREATE POLICY savegeo_runtime_access ON {table} FOR ALL TO PUBLIC "
            "USING (current_user = 'savegeo_runtime') "
            "WITH CHECK (current_user = 'savegeo_runtime')"
        )


def downgrade():
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS savegeo_runtime_access ON {table}")
