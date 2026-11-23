"""Add reported credit event ledger and mobile field capture queue."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0052_dmrv_advanced_review_tools"
down_revision = "0051_dmrv_integrity_workspace"
branch_labels = None
depends_on = None


def _has_table(table_name, required_columns, required_constraints):
    """Accept an already-applied additive DDL patch only when its shape matches."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name, schema="public"):
        return False
    actual_columns = {column["name"] for column in inspector.get_columns(table_name, schema="public")}
    missing = sorted(set(required_columns) - actual_columns)
    if missing:
        raise RuntimeError(
            f"Existing public.{table_name} is missing required columns: {', '.join(missing)}"
        )
    constraints = set()
    primary_key = inspector.get_pk_constraint(table_name, schema="public")
    if primary_key.get("name"):
        constraints.add(primary_key["name"])
    for getter in (inspector.get_unique_constraints, inspector.get_check_constraints, inspector.get_foreign_keys):
        constraints.update(
            item["name"] for item in getter(table_name, schema="public") if item.get("name")
        )
    missing_constraints = sorted(set(required_constraints) - constraints)
    if missing_constraints:
        raise RuntimeError(
            f"Existing public.{table_name} is missing required constraints: "
            f"{', '.join(missing_constraints)}"
        )
    return True


def _ensure_index(table_name, index_name, columns):
    inspector = sa.inspect(op.get_bind())
    existing = {index["name"] for index in inspector.get_indexes(table_name, schema="public")}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, schema="public")


def upgrade():
    if not _has_table(
        "dmrv_credit_events",
        (
            "event_id", "project_id", "batch_reference", "event_type", "quantity", "owner_from",
            "owner_to", "registry", "external_transaction_id", "effective_date", "evidence",
            "created_by", "created_at",
        ),
        (
            "dmrv_credit_events_pkey", "dmrv_credit_events_project_id_fkey",
            "uq_dmrv_credit_event_project_id", "uq_dmrv_credit_external_tx",
            "ck_dmrv_credit_quantity_positive", "ck_dmrv_credit_event_type",
        ),
    ):
        op.create_table(
            "dmrv_credit_events",
            sa.Column("event_id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("batch_reference", sa.String(120), nullable=False),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("quantity", sa.BigInteger, nullable=False),
            sa.Column("owner_from", sa.String(200)),
            sa.Column("owner_to", sa.String(200)),
            sa.Column("registry", sa.String(100), nullable=False),
            sa.Column("external_transaction_id", sa.String(160), nullable=False),
            sa.Column("effective_date", sa.Date, nullable=False),
            sa.Column("evidence", JSONB, nullable=False),
            sa.Column("created_by", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "event_id", name="uq_dmrv_credit_event_project_id"),
            sa.UniqueConstraint("project_id", "registry", "external_transaction_id", name="uq_dmrv_credit_external_tx"),
            sa.CheckConstraint("quantity > 0", name="ck_dmrv_credit_quantity_positive"),
            sa.CheckConstraint(
                "event_type IN ('issuance_reported','transfer_reported','use_reported','retirement_reported','cancellation_reported')",
                name="ck_dmrv_credit_event_type",
            ),
            schema="public",
        )
    _ensure_index("dmrv_credit_events", "ix_dmrv_credit_events_project_id", ["project_id"])
    _ensure_index("dmrv_credit_events", "ix_dmrv_credit_events_batch_reference", ["batch_reference"])

    if not _has_table(
        "dmrv_field_captures",
        (
            "capture_id", "project_id", "plot_code", "plot_id", "observed_at", "latitude", "longitude",
            "gps_accuracy_m", "observation", "photo_document_id", "qa_flags", "created_by", "created_at",
        ),
        (
            "dmrv_field_captures_pkey", "dmrv_field_captures_project_id_fkey",
            "dmrv_field_captures_plot_id_fkey", "dmrv_field_captures_photo_document_id_fkey",
            "uq_dmrv_field_capture_project_id", "ck_dmrv_field_capture_lat", "ck_dmrv_field_capture_lon",
        ),
    ):
        op.create_table(
            "dmrv_field_captures",
            sa.Column("capture_id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("dmrv_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("plot_code", sa.String(64), nullable=False),
            sa.Column("plot_id", sa.String(36), sa.ForeignKey("dmrv_survey_plots.id", ondelete="SET NULL")),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("latitude", sa.Float, nullable=False),
            sa.Column("longitude", sa.Float, nullable=False),
            sa.Column("gps_accuracy_m", sa.Float),
            sa.Column("observation", sa.Text, nullable=False),
            sa.Column("photo_document_id", sa.String(36), sa.ForeignKey("dmrv_integrity_documents.id", ondelete="SET NULL")),
            sa.Column("qa_flags", JSONB, nullable=False),
            sa.Column("created_by", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "capture_id", name="uq_dmrv_field_capture_project_id"),
            sa.CheckConstraint("latitude BETWEEN -85 AND 85", name="ck_dmrv_field_capture_lat"),
            sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_dmrv_field_capture_lon"),
            schema="public",
        )
    _ensure_index("dmrv_field_captures", "ix_dmrv_field_captures_project_id", ["project_id"])
    _ensure_index("dmrv_field_captures", "ix_dmrv_field_captures_plot_id", ["plot_id"])
    if op.get_bind().dialect.name == "postgresql":
        for table in ("dmrv_credit_events", "dmrv_field_captures"):
            op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
            for role in ("anon", "authenticated"):
                op.execute(
                    f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN REVOKE ALL ON public.{table} FROM {role}; END IF; END $$"
                )
        op.execute(
            "CREATE OR REPLACE FUNCTION public.dmrv_reject_credit_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'dMRV credit events are append-only'; END $$"
        )
        op.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tr_dmrv_credit_events_append_only' "
            "AND tgrelid = 'public.dmrv_credit_events'::regclass AND NOT tgisinternal) THEN "
            "EXECUTE 'CREATE TRIGGER tr_dmrv_credit_events_append_only BEFORE UPDATE OR DELETE ON public.dmrv_credit_events "
            "FOR EACH ROW EXECUTE FUNCTION public.dmrv_reject_credit_event_mutation()'; END IF; END $$"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS tr_dmrv_credit_events_append_only ON dmrv_credit_events")
        op.execute("DROP FUNCTION IF EXISTS dmrv_reject_credit_event_mutation()")
    op.drop_table("dmrv_field_captures")
    op.drop_table("dmrv_credit_events")