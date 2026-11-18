"""Add leases and retry scheduling to field-prediction runs."""
from alembic import op
import sqlalchemy as sa

revision = "0060_field_pred_queue"
down_revision = "0059_admin_user_mfa_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("carbon_calibration_runs", sa.Column("worker_id", sa.String(96), nullable=True))
    op.add_column("carbon_calibration_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("carbon_calibration_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("carbon_calibration_runs", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("carbon_calibration_runs", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("carbon_calibration_runs", sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_carbon_calibration_run_queue", "carbon_calibration_runs", ["method", "status", "run_after"])
    op.execute("""
        UPDATE carbon_calibration_runs
        SET status='queued', progress=0, error=NULL, completed_at=NULL,
            worker_id=NULL, lease_expires_at=NULL, heartbeat_at=NULL, run_after=now()
        WHERE method='gee_field_prediction' AND status='running'
    """)


def downgrade():
    op.drop_index("ix_carbon_calibration_run_queue", table_name="carbon_calibration_runs")
    for name in ("run_after", "max_attempts", "attempts", "heartbeat_at", "lease_expires_at", "worker_id"):
        op.drop_column("carbon_calibration_runs", name)
