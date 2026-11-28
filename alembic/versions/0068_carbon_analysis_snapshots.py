"""Persist append-only carbon result snapshots for statistics export."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0068_carbon_analysis_snapshots"
down_revision: str | None = "0067_viewer_dmrv_feature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "carbon_analysis_snapshots",
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("owner_key", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_provenance.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("analysis_id"),
    )
    op.create_index("ix_carbon_analysis_snapshots_owner_key", "carbon_analysis_snapshots", ["owner_key"])
    op.execute("""
        CREATE FUNCTION prevent_carbon_snapshot_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'carbon analysis snapshots are immutable';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER carbon_analysis_snapshots_immutable
        BEFORE UPDATE OR DELETE ON carbon_analysis_snapshots
        FOR EACH ROW EXECUTE FUNCTION prevent_carbon_snapshot_mutation();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS carbon_analysis_snapshots_immutable ON carbon_analysis_snapshots")
    op.execute("DROP FUNCTION IF EXISTS prevent_carbon_snapshot_mutation()")
    op.drop_index("ix_carbon_analysis_snapshots_owner_key", table_name="carbon_analysis_snapshots")
    op.drop_table("carbon_analysis_snapshots")
