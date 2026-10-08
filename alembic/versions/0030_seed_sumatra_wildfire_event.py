"""Configure the published Sumatra wildfire monitoring explorer.

Observation counts and acquisition times are populated by the explicit FIRMS
sync, never invented by the event seed.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_sumatra_wildfire_event"
down_revision: str | None = "0029_imagery_provider_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_SLUG = "sumatra-2026"
PROVINCES = [
    "Aceh", "Sumatera Utara", "Sumatera Barat", "Riau", "Jambi",
    "Sumatera Selatan", "Bengkulu", "Lampung", "Kepulauan Bangka Belitung", "Kepulauan Riau",
]
PROVINCE_CODES = ["11", "12", "13", "14", "15", "16", "17", "18", "19", "21"]
# Includes Sumatra and its offshore provinces, including eastern Kepulauan Riau.
BBOX = [94.9, -6.1, 109.3, 6.1]


def upgrade() -> None:
    connection = op.get_bind()
    events = sa.table(
        "disaster_events",
        sa.column("id", sa.Integer()),
        *[sa.column(key, sa.String()) for key in (
            "slug", "name", "short_title", "disaster_type", "location_name", "status",
            "severity", "source", "thumbnail", "country_code", "boundary_source",
        )],
        *[sa.column(key, postgresql.JSONB()) for key in (
            "province", "district", "province_codes", "city_codes", "bbox", "source_ids",
            "hotspot_dataset_ids", "burned_area_dataset_ids", "limitations",
        )],
        *[sa.column(key, sa.Date()) for key in ("event_date", "start_date", "monitoring_from")],
        *[sa.column(key, sa.DateTime(timezone=True)) for key in ("published_at", "created_at", "updated_at")],
        sa.column("description", sa.Text()), sa.column("methodology", sa.Text()),
        sa.column("year", sa.Integer()), sa.column("center_lat", sa.Float()),
        sa.column("center_lon", sa.Float()), sa.column("default_zoom", sa.Integer()),
        sa.column("is_public", sa.Boolean()), sa.column("is_featured", sa.Boolean()),
    )
    existing = connection.execute(sa.select(events.c.id).where(events.c.slug == EVENT_SLUG)).scalar_one_or_none()
    coverage = {
        "province": PROVINCES, "province_codes": PROVINCE_CODES, "bbox": BBOX,
        "center_lat": 0.0, "center_lon": 102.1, "default_zoom": 5,
        "boundary_source": "Peta Administratif",
    }
    if existing is not None:
        connection.execute(sa.update(events).where(events.c.id == existing).values(**coverage))
        return
    now = datetime.now(UTC)
    connection.execute(events.insert().values(
        **coverage, slug=EVENT_SLUG, name="Karhutla Sumatra 2026", short_title="Sumatra 2026",
        disaster_type="forest_fire", location_name="Sumatra", status="published", severity="medium",
        district=[], city_codes=[],
        event_date=date(2026, 8, 1), start_date=date(2026, 8, 1), monitoring_from=date(2026, 8, 1),
        published_at=now, created_at=now, updated_at=now, year=2026, country_code="ID",
        source="NASA FIRMS / NASA EOSDIS LANCE", source_ids=["nasa-firms"],
        hotspot_dataset_ids=["nasa-firms"], burned_area_dataset_ids=[],
        thumbnail="/images/land-change-disturbance.jpg",
        description="Pemantauan deteksi panas dan sumber data kebakaran hutan dan lahan di sepuluh provinsi Sumatra.",
        methodology="Observasi NASA FIRMS disimpan dan disaring dengan batas daratan Sumatra; GFAS diintegrasikan pada batas provinsi.",
        limitations=["Hotspot adalah indikasi anomali termal, bukan luas terbakar; aerosol CAMS mencakup sumber selain kebakaran."],
        is_public=True, is_featured=False,
    ))


def downgrade() -> None:
    # Keep observations and the monitoring event when rolling back code.
    pass
