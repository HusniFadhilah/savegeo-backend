import datetime as dt
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql
from shapely.geometry import shape

from app.api.routes import wildfire_events
from app.services import wildfire_hotspot_service
from app.services import wildfire_geometry
from app.services.wildfire_catalog import PROVINCE_MASK_EVENTS


def test_national_scope_migration_seeds_published_country_monitor():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0034_national_wildfire_scope.py"
    spec = importlib.util.spec_from_file_location("national_wildfire_scope", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    with patch.object(migration.op, "get_bind", return_value=db):
        migration.upgrade()
    insert = next(call.args[0] for call in db.execute.call_args_list if call.args[0].is_insert)
    row = insert.compile(dialect=postgresql.dialect()).params

    assert row["slug"] == "all"
    assert row["status"] == "published"
    assert row["is_public"] is False
    assert row["province_codes"] == [code for code, _ in migration.PROVINCES]
    assert len(row["province_codes"]) == len(migration.PROVINCES) == 38
    assert row["bbox"] == [94.0, -11.5, 142.0, 7.0]
    assert "all" in PROVINCE_MASK_EVENTS


def test_national_hotspot_cache_initializes_latest_seven_days():
    today = dt.datetime.now(dt.UTC).date()
    event = SimpleNamespace(
        slug="all",
        monitoring_from=dt.date(2026, 1, 1),
        monitoring_to=None,
        hotspot_last_synced_at=None,
    )
    db = MagicMock()

    with patch.object(wildfire_hotspot_service, "sync_event_hotspots", return_value={"stored": 1}) as sync:
        result = wildfire_hotspot_service.ensure_initial_sync(db, event)

    assert result == {"stored": 1}
    sync.assert_called_once_with(
        db,
        event,
        from_date=today - dt.timedelta(days=6),
        to_date=today,
    )


def test_hotspot_default_date_window_uses_current_week_instead_of_stale_last_data():
    today = dt.datetime.now(dt.UTC).date()
    event = SimpleNamespace(last_data_at=dt.datetime(2026, 10, 6, tzinfo=dt.UTC))

    assert wildfire_events._period_dates(event, None, None) == (
        (today - dt.timedelta(days=today.weekday())).isoformat(),
        today.isoformat(),
    )


def test_static_province_boundaries_cover_all_38_unique_codes():
    geometries = wildfire_geometry.province_geometries()

    assert len(geometries) == 38
    assert all(shape(feature["geometry"]).is_valid for feature in geometries.values())
    assert geometries["93"]["properties"]["PROVINSI"] == "Papua Selatan"
    assert geometries["96"]["properties"]["PROVINSI"] == "Papua Barat Daya"
    assert wildfire_events._admin_geometry("province", "93")["properties"]["KODE_PROV"] == "93"
