"""Add a persisted nationwide scope for near-real-time wildfire monitoring."""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_national_wildfire_scope"
down_revision: str | None = "0033_carbon_inference_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVINCES = [
    ("11", "Aceh"),
    ("12", "Sumatera Utara"),
    ("13", "Sumatera Barat"),
    ("14", "Riau"),
    ("15", "Jambi"),
    ("16", "Sumatera Selatan"),
    ("17", "Bengkulu"),
    ("18", "Lampung"),
    ("19", "Kepulauan Bangka Belitung"),
    ("21", "Kepulauan Riau"),
    ("31", "DKI Jakarta"),
    ("32", "Jawa Barat"),
    ("33", "Jawa Tengah"),
    ("34", "DI Yogyakarta"),
    ("35", "Jawa Timur"),
    ("36", "Banten"),
    ("51", "Bali"),
    ("52", "Nusa Tenggara Barat"),
    ("53", "Nusa Tenggara Timur"),
    ("61", "Kalimantan Barat"),
    ("62", "Kalimantan Tengah"),
    ("63", "Kalimantan Selatan"),
    ("64", "Kalimantan Timur"),
    ("65", "Kalimantan Utara"),
    ("71", "Sulawesi Utara"),
    ("72", "Sulawesi Tengah"),
    ("73", "Sulawesi Selatan"),
    ("74", "Sulawesi Tenggara"),
    ("75", "Gorontalo"),
    ("76", "Sulawesi Barat"),
    ("81", "Maluku"),
    ("82", "Maluku Utara"),
    ("91", "Papua"),
    ("92", "Papua Barat"),
    ("93", "Papua Selatan"),
    ("94", "Papua Tengah"),
    ("95", "Papua Pegunungan"),
    ("96", "Papua Barat Daya"),
]


def upgrade() -> None:
    events = sa.table(
        "disaster_events",
        sa.column("id", sa.Integer()),
        *[
            sa.column(name, sa.String())
            for name in (
                "slug",
                "name",
                "short_title",
                "disaster_type",
                "location_name",
                "status",
                "severity",
                "source",
                "thumbnail",
                "country_code",
                "boundary_source",
            )
        ],
        *[
            sa.column(name, postgresql.JSONB())
            for name in (
                "province",
                "district",
                "province_codes",
                "city_codes",
                "bbox",
                "source_ids",
                "hotspot_dataset_ids",
                "burned_area_dataset_ids",
                "limitations",
            )
        ],
        *[sa.column(name, sa.Date()) for name in ("event_date", "start_date", "monitoring_from")],
        *[
            sa.column(name, sa.DateTime(timezone=True))
            for name in ("published_at", "created_at", "updated_at")
        ],
        sa.column("description", sa.Text()),
        sa.column("methodology", sa.Text()),
        sa.column("year", sa.Integer()),
        sa.column("center_lat", sa.Float()),
        sa.column("center_lon", sa.Float()),
        sa.column("default_zoom", sa.Integer()),
        sa.column("is_public", sa.Boolean()),
        sa.column("is_featured", sa.Boolean()),
    )
    connection = op.get_bind()
    exists = connection.execute(sa.select(events.c.id).where(events.c.slug == "all")).scalar_one_or_none()
    if exists is not None:
        return

    now = datetime.now(UTC)
    province_codes = [code for code, _ in PROVINCES]
    connection.execute(
        events.insert().values(
            slug="all",
            name="Hotspot Karhutla Seluruh Indonesia",
            short_title="Seluruh Indonesia",
            disaster_type="forest_fire",
            location_name="Indonesia",
            status="published",
            severity="medium",
            province=[name for _, name in PROVINCES],
            district=[],
            province_codes=province_codes,
            city_codes=[],
            bbox=[94.0, -11.5, 142.0, 7.0],
            center_lat=-2.5,
            center_lon=118.0,
            default_zoom=4,
            boundary_source="Batas administratif Indonesia",
            event_date=date(2026, 1, 1),
            start_date=date(2026, 1, 1),
            monitoring_from=date(2026, 1, 1),
            published_at=now,
            created_at=now,
            updated_at=now,
            year=2026,
            country_code="ID",
            source="NASA FIRMS · Copernicus CAMS/GFAS",
            source_ids=["nasa-firms", "cams-gfas", "cams-ifs"],
            hotspot_dataset_ids=["nasa-firms"],
            burned_area_dataset_ids=[],
            thumbnail="/images/land-change-disturbance.jpg",
            description="Pemantauan titik panas satelit untuk seluruh wilayah Indonesia.",
            methodology=(
                "Hotspot FIRMS disaring dengan gabungan batas provinsi yang tersedia pada "
                "data administrasi. Arsip CAMS/GFAS disimpan terpisah sebagai raster bertanggal."
            ),
            limitations=[
                "Hotspot adalah indikasi anomali termal, bukan jumlah kejadian maupun luas terbakar.",
                "Ketersediaan FIRMS mengikuti waktu lintas satelit dan pembaruan NASA.",
                "GFAS adalah estimasi emisi berbasis grid; aerosol CAMS juga dapat berasal dari sumber lain.",
                "Cakupan provinsi mengikuti batas administrasi yang tersedia pada server.",
            ],
            # This monitoring scope is exposed through the wildfire API only;
            # it is not a single incident for the general disaster feed.
            is_public=False,
            is_featured=False,
        )
    )


def downgrade() -> None:
    # Keep persisted hotspot and archive data if this seed migration is rolled back.
    pass
