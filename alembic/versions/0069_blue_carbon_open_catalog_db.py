"""Move curated open blue-carbon catalogue rows into system_config."""

from collections.abc import Sequence
import json

import sqlalchemy as sa
from alembic import op

from app.registries.blue_carbon_open_datasets import BLUE_CARBON_OPEN_DATASETS

revision: str = "0069_blue_carbon_open_catalog_db"
down_revision: str | None = "0068_carbon_analysis_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    config = sa.table(
        "system_config",
        sa.column("key", sa.String(128)),
        sa.column("value", sa.Text()),
        sa.column("value_type", sa.String(16)),
        sa.column("category", sa.String(64)),
        sa.column("label", sa.String(256)),
        sa.column("description", sa.Text()),
        sa.column("is_public", sa.Boolean()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    key = "carbon.bluecarbon_profiles"
    row = connection.execute(
        sa.select(config.c.key, config.c.value).where(config.c.key == key)
    ).mappings().first()

    if row is None:
        connection.execute(
            config.insert().values(
                key=key,
                value=json.dumps(BLUE_CARBON_OPEN_DATASETS, ensure_ascii=False),
                value_type="json",
                category="carbon",
                label="Katalog Blue Carbon",
                description=(
                    "Sumber terbuka blue carbon pada katalog pengguna; nilai pool kosong "
                    "bila sumber tidak memberi angka ringkasan."
                ),
                is_public=False,
            )
        )
        return

    current_value = row["value"]
    if current_value is None:
        current_value = "[]"
    try:
        existing = json.loads(current_value)
    except (TypeError, ValueError):
        # Do not replace an admin value that is malformed; the admin can repair it.
        return
    if not isinstance(existing, list):
        return

    known_ids = {
        str(record.get("id"))
        for record in existing
        if isinstance(record, dict) and record.get("id") is not None
    }
    merged = [*existing]
    merged.extend(
        record
        for record in BLUE_CARBON_OPEN_DATASETS
        if str(record.get("id")) not in known_ids
    )
    if len(merged) != len(existing):
        connection.execute(
            config.update()
            .where(config.c.key == key)
            .values(value=json.dumps(merged, ensure_ascii=False), updated_at=sa.func.now())
        )


def downgrade() -> None:
    # Catalogue rows may have been edited since the migration, so a downgrade
    # intentionally leaves the persisted records intact.
    pass
