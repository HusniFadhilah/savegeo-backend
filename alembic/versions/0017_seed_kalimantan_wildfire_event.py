"""Seed the published Kalimantan 2026 wildfire event used by the public explorer.

The frontend has a lightweight fallback for this event, but the detail API is
database-backed. Keeping the canonical event in the database prevents the
public wildfire endpoints from returning 404 after a fresh deployment.
"""

from collections.abc import Sequence
from datetime import date, datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_seed_kalimantan_wildfire_event"
down_revision: str | None = "0016_analysis_outputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EVENT_SLUG = "kalimantan-2026"


def upgrade() -> None:
    connection = op.get_bind()
    events = sa.table(
        "disaster_events",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("disaster_type", sa.String()),
        sa.column("location_name", sa.String()),
        sa.column("province", postgresql.JSONB()),
        sa.column("district", postgresql.JSONB()),
        sa.column("event_date", sa.Date()),
        sa.column("start_date", sa.Date()),
        sa.column("status", sa.String()),
        sa.column("severity", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("source", sa.String()),
        sa.column("thumbnail", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("slug", sa.String()),
        sa.column("short_title", sa.String()),
        sa.column("monitoring_from", sa.Date()),
        sa.column("monitoring_to", sa.Date()),
        sa.column("published_at", sa.DateTime(timezone=True)),
        sa.column("last_data_at", sa.DateTime(timezone=True)),
        sa.column("last_synced_at", sa.DateTime(timezone=True)),
        sa.column("year", sa.Integer()),
        sa.column("country_code", sa.String()),
        sa.column("province_codes", postgresql.JSONB()),
        sa.column("city_codes", postgresql.JSONB()),
        sa.column("bbox", postgresql.JSONB()),
        sa.column("center_lat", sa.Float()),
        sa.column("center_lon", sa.Float()),
        sa.column("default_zoom", sa.Integer()),
        sa.column("source_ids", postgresql.JSONB()),
        sa.column("hotspot_dataset_ids", postgresql.JSONB()),
        sa.column("burned_area_dataset_ids", postgresql.JSONB()),
        sa.column("boundary_source", sa.String()),
        sa.column("methodology", sa.Text()),
        sa.column("limitations", postgresql.JSONB()),
        sa.column("is_featured", sa.Boolean()),
        sa.column("is_public", sa.Boolean()),
    )

    already_seeded = connection.execute(
        sa.select(events.c.id).where(events.c.slug == EVENT_SLUG)
    ).scalar_one_or_none()
    if already_seeded is not None:
        return

    synced_at = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)
    connection.execute(
        events.insert().values(
            name="Karhutla Kalimantan 2026",
            disaster_type="forest_fire",
            location_name="Kalimantan",
            province=[
                "Kalimantan Barat",
                "Kalimantan Tengah",
                "Kalimantan Selatan",
                "Kalimantan Timur",
                "Kalimantan Utara",
            ],
            district=[],
            event_date=date(2026, 1, 1),
            start_date=date(2026, 1, 1),
            status="published",
            severity="high",
            description=(
                "Pemantauan deteksi panas dan sumber data kebakaran hutan dan lahan "
                "di Kalimantan."
            ),
            source="NASA FIRMS / Kementerian Kehutanan",
            thumbnail="/images/disaster-water-risk.jpg",
            created_at=synced_at,
            updated_at=synced_at,
            slug=EVENT_SLUG,
            short_title="Kalimantan 2026",
            monitoring_from=date(2026, 8, 20),
            published_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            last_data_at=datetime(2026, 8, 27, 23, 59, tzinfo=timezone.utc),
            last_synced_at=synced_at,
            year=2026,
            country_code="ID",
            province_codes=["61", "62", "63", "64", "65"],
            city_codes=[],
            bbox=[108.8, -4.6, 119.4, 4.4],
            center_lat=-1.7,
            center_lon=113.4,
            default_zoom=5,
            source_ids=["nasa-firms", "laporan-kemenhut"],
            hotspot_dataset_ids=["nasa-firms"],
            burned_area_dataset_ids=[],
            boundary_source="SP3STAB",
            methodology=(
                "Deteksi panas satelit dikurasi sebagai event pemantauan; tidak "
                "disamakan dengan incident atau area terbakar."
            ),
            limitations=[
                "Hotspot adalah indikasi anomali termal dan memerlukan verifikasi lapangan."
            ],
            is_featured=True,
            is_public=True,
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM disaster_events "
            "WHERE slug = :slug AND name = :name AND created_by IS NULL"
        ),
        {"slug": EVENT_SLUG, "name": "Karhutla Kalimantan 2026"},
    )
