"""Seed five Indonesian regional wildfire scopes awaiting data verification.

New events remain drafts until an explicit operation verifies actual FIRMS,
province boundaries, GFAS and CAMS rasters and publishes the complete event.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031_indonesia_wildfire_events"
down_revision: str | None = "0030_sumatra_wildfire_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPES = [
    ("jawa-2026", "Jawa", ["31", "32", "33", "34", "35", "36"],
     ["DKI Jakarta", "Jawa Barat", "Jawa Tengah", "Daerah Istimewa Yogyakarta", "Jawa Timur", "Banten"],
     [105.0, -9.0, 116.4, -4.9], -7.3, 110.5,
     "Termasuk pemantauan regional sekitar kejadian Gunung Argopuro yang dilaporkan BNPB pada 8 September 2026."),
    ("sulawesi-2026", "Sulawesi", ["71", "72", "73", "74", "75", "76"],
     ["Sulawesi Utara", "Sulawesi Tengah", "Sulawesi Selatan", "Sulawesi Tenggara", "Gorontalo", "Sulawesi Barat"],
     [117.5, -7.6, 127.3, 5.7], -1.0, 122.0,
     "Termasuk pemantauan regional sekitar karhutla Majene pada 19 September 2026 yang dilaporkan BNPB."),
    ("bali-nusa-tenggara-2026", "Bali & Nusa Tenggara", ["51", "52", "53"],
     ["Bali", "Nusa Tenggara Barat", "Nusa Tenggara Timur"],
     [114.4, -11.1, 125.3, -6.4], -9.0, 120.0,
     "Termasuk pemantauan regional sekitar penanganan karhutla Loloan, Lombok Utara, pada 24 September 2026 yang dilaporkan BNPB."),
    ("maluku-2026", "Maluku", ["81", "82"], ["Maluku", "Maluku Utara"],
     [124.1, -8.6, 135.0, 2.9], -3.0, 129.5,
     "Termasuk pemantauan regional sekitar laporan karhutla Maluku Utara dalam rekap BNPB tanggal 2 Oktober 2026."),
    ("papua-2026", "Papua", ["91", "92"], ["Papua", "Papua Barat"],
     [128.8, -9.3, 141.1, 1.2], -4.0, 136.0,
     "Termasuk pemantauan regional sekitar karhutla Nabire pada 19 September 2026 yang dilaporkan BNPB. Batas provinsi layanan masih memakai cakupan sebelum pemekaran 2022."),
]


def upgrade() -> None:
    connection = op.get_bind()
    events = sa.table(
        "disaster_events", sa.column("id", sa.Integer()),
        *[sa.column(key, sa.String()) for key in (
            "slug", "name", "short_title", "disaster_type", "location_name", "status",
            "severity", "source", "thumbnail", "country_code", "boundary_source",
        )],
        *[sa.column(key, postgresql.JSONB()) for key in (
            "province", "district", "province_codes", "city_codes", "bbox", "source_ids",
            "hotspot_dataset_ids", "burned_area_dataset_ids", "limitations",
        )],
        *[sa.column(key, sa.Date()) for key in ("event_date", "start_date", "monitoring_from")],
        *[sa.column(key, sa.DateTime(timezone=True)) for key in ("created_at", "updated_at")],
        sa.column("description", sa.Text()), sa.column("methodology", sa.Text()),
        sa.column("year", sa.Integer()), sa.column("center_lat", sa.Float()),
        sa.column("center_lon", sa.Float()), sa.column("default_zoom", sa.Integer()),
        sa.column("is_public", sa.Boolean()), sa.column("is_featured", sa.Boolean()),
    )
    now = datetime.now(UTC)
    for slug, label, codes, names, bbox, lat, lon, description in SCOPES:
        existing = connection.execute(sa.select(events.c.id).where(events.c.slug == slug)).scalar_one_or_none()
        if existing is not None:
            continue  # Preserve admin edits, publication decisions and satellite data.
        limitations = [
            "Hotspot adalah anomali termal, bukan jumlah kejadian maupun luas terbakar.",
            "CAMS aerosol mencakup sumber selain kebakaran; GFAS adalah estimasi emisi grid regional.",
            "Rujukan BNPB adalah contoh kejadian; seluruh hotspot wilayah tidak diatribusikan ke kejadian tersebut.",
        ]
        boundary_source = "Peta Administratif"
        if slug == "papua-2026":
            boundary_source += " · batas arsip Papua/Papua Barat sebelum pemekaran 2022"
            limitations.append("Statistik provinsi Papua memakai dua cakupan arsip sebelum pemekaran 2022, bukan enam provinsi saat ini.")
        connection.execute(events.insert().values(
            slug=slug, name=f"Karhutla {label} 2026", short_title=f"{label} 2026",
            disaster_type="forest_fire", location_name=label, status="draft", severity="medium",
            province=names, province_codes=codes, district=[], city_codes=[], bbox=bbox,
            center_lat=lat, center_lon=lon, default_zoom=5, boundary_source=boundary_source,
            event_date=date(2026, 9, 8), start_date=date(2026, 9, 8), monitoring_from=date(2026, 9, 8),
            created_at=now, updated_at=now, year=2026, country_code="ID",
            source="NASA FIRMS · Copernicus CAMS/GFAS · BNPB", source_ids=["nasa-firms", "cams-gfas", "cams-ifs", "bnpb"],
            hotspot_dataset_ids=["nasa-firms"], burned_area_dataset_ids=[],
            thumbnail="/images/land-change-disturbance.jpg", description=description,
            methodology="Deteksi panas disaring dengan gabungan batas provinsi yang sama dengan agregasi GFAS. Pemantauan regional dimulai 8 September 2026.",
            limitations=limitations, is_public=False, is_featured=False,
        ))


def downgrade() -> None:
    # Satellite observations and curated event metadata must not be deleted.
    pass
