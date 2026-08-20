"""Disaster Intelligence Dashboard schema

Adds: users (separate app-user accounts, JWT-gated, not admin_users),
disaster_events, disaster_aois, satellite_imagery, analysis_runs,
analysis_results, hotspots. Publish gate lives on analysis_results
(is_published) and hotspots (is_published) - User-facing routes must always
filter on these. Admin actions log into the existing `audit_logs` table
(resource_type="disaster_event"|"disaster_analysis"|...), no new audit table
needed. New RBAC permission codes (disaster.*) are seeded separately via
`scripts/seed_rbac.py`, not by this migration.

Revision ID: 0006_disaster_intelligence
Revises: 0005_satellite_tech_fields
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_disaster_intelligence"
down_revision: Union[str, None] = "0005_satellite_tech_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(128), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "disaster_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("disaster_type", sa.String(30), nullable=False),
        sa.Column("location_name", sa.String(255)),
        sa.Column("province", postgresql.JSONB()),
        sa.Column("district", postgresql.JSONB()),
        sa.Column("event_date", sa.Date()),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("severity", sa.String(10)),
        sa.Column("description", sa.Text()),
        sa.Column("source", sa.String(255)),
        sa.Column("thumbnail", sa.String(500)),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_disaster_events_status", "disaster_events", ["status"])

    op.create_table(
        "disaster_aois",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("geojson", postgresql.JSONB(), nullable=False),
        sa.Column("area_ha", sa.Float()),
        sa.Column("centroid", postgresql.JSONB()),
        sa.Column("bbox", postgresql.JSONB()),
        sa.Column("source", sa.String(30), server_default="draw", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_disaster_aois_event_id", "disaster_aois", ["event_id"])

    op.create_table(
        "satellite_imagery",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phase", sa.String(10), nullable=False),
        sa.Column("satellite", sa.String(50), nullable=False),
        sa.Column("acquisition_date", sa.Date(), nullable=False),
        sa.Column("sensor", sa.String(50)),
        sa.Column("resolution_m", sa.Float()),
        sa.Column("cloud_coverage_pct", sa.Float()),
        sa.Column("data_source", sa.String(100)),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("preview_tile_url", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_satellite_imagery_event_id", "satellite_imagery", ["event_id"])

    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(20)),
        sa.Column("aoi_id", sa.Integer(), sa.ForeignKey("disaster_aois.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pre_imagery_id", sa.Integer(), sa.ForeignKey("satellite_imagery.id", ondelete="SET NULL")),
        sa.Column("post_imagery_id", sa.Integer(), sa.ForeignKey("satellite_imagery.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_analysis_runs_event_id", "analysis_runs", ["event_id"])
    op.create_index("ix_analysis_runs_status", "analysis_runs", ["status"])

    op.create_table(
        "analysis_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("tile_url", sa.String(1000)),
        sa.Column("statistics", postgresql.JSONB()),
        sa.Column("features", postgresql.JSONB()),
        sa.Column("legend", postgresql.JSONB()),
        sa.Column("confidence_summary", postgresql.JSONB()),
        sa.Column("is_published", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("published_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("publication_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_analysis_results_run_id", "analysis_results", ["run_id"])
    op.create_index("ix_analysis_results_is_published", "analysis_results", ["is_published"])

    op.create_table(
        "hotspots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("disaster_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_result_id", sa.Integer(), sa.ForeignKey("analysis_results.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("impact_level", sa.String(10), nullable=False),
        sa.Column("geojson", postgresql.JSONB(), nullable=False),
        sa.Column("stats", postgresql.JSONB()),
        sa.Column("is_published", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_hotspots_event_id", "hotspots", ["event_id"])
    op.create_index("ix_hotspots_is_published", "hotspots", ["is_published"])


def downgrade() -> None:
    op.drop_table("hotspots")
    op.drop_table("analysis_results")
    op.drop_table("analysis_runs")
    op.drop_table("satellite_imagery")
    op.drop_table("disaster_aois")
    op.drop_table("disaster_events")
    op.drop_table("users")
