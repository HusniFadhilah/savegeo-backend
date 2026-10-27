import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services import cams_archive_service as cams
from app.services.wildfire_catalog import EVENT_ISLANDS, OFFICIAL_REPORTS, PROVINCE_MASK_EVENTS
from app.services.wildfire_geometry import _prepared_provinces, is_feature_in_event_scope


def migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0031_seed_indonesia_wildfire_events.py"
    spec = importlib.util.spec_from_file_location("indonesia_wildfire_seed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_five_scopes_are_drafts_without_invented_observations(monkeypatch):
    seed = migration()
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    monkeypatch.setattr(seed.op, "get_bind", lambda: db)
    seed.upgrade()
    inserts = [call.args[0].compile(dialect=postgresql.dialect()).params
               for call in db.execute.call_args_list if call.args[0].is_insert]
    assert {entry["slug"] for entry in inserts} == set(PROVINCE_MASK_EVENTS) - {"all"}
    assert len(inserts) == 5
    for entry in inserts:
        assert entry["status"] == "draft"
        assert entry["is_public"] is False
        assert len(entry["province_codes"]) == len(entry["province"])
        assert not {"published_at", "last_data_at", "last_synced_at", "hotspot_last_synced_at"} & entry.keys()
    papua = next(entry for entry in inserts if entry["slug"] == "papua-2026")
    assert "sebelum pemekaran 2022" in papua["boundary_source"]


def test_seed_preserves_existing_admin_decisions(monkeypatch):
    seed = migration()
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = 42
    monkeypatch.setattr(seed.op, "get_bind", lambda: db)
    seed.upgrade()
    assert all(not call.args[0].is_insert and not call.args[0].is_update for call in db.execute.call_args_list)


def test_scopes_have_official_incident_references():
    assert set(OFFICIAL_REPORTS) == set(PROVINCE_MASK_EVENTS) - {"all"}
    for slug, reports in OFFICIAL_REPORTS.items():
        assert slug in EVENT_ISLANDS
        assert reports and all("bnpb.go.id/" in report["url"] for report in reports)
        assert all(report["event_date"].startswith("2026-") for report in reports)


@pytest.fixture
def province_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cams, "event_directory", lambda slug: tmp_path / slug)
    directory = tmp_path / "boundaries"
    directory.mkdir()
    payload = {"type": "Polygon", "coordinates": [[[120, -3], [121, -3], [121, -2], [120, -2], [120, -3]]]}
    (directory / "province-73.json").write_text(json.dumps(payload))
    _prepared_provinces.cache_clear()
    yield directory
    _prepared_provinces.cache_clear()


@pytest.mark.parametrize("slug", sorted(PROVINCE_MASK_EVENTS - {"all"}))
def test_all_new_scopes_use_validated_province_mask(province_cache, slug):
    event = SimpleNamespace(slug=slug, province_codes=["73"])
    inside = {"geometry": {"type": "Point", "coordinates": [120.5, -2.5]}}
    outside = {"geometry": {"type": "Point", "coordinates": [121.5, -2.5]}}
    assert is_feature_in_event_scope(event, inside)
    assert not is_feature_in_event_scope(event, outside)


def test_national_scope_uses_indonesia_land_mask_without_live_province_api():
    event = SimpleNamespace(slug="all", province_codes=["11", "91", "96"])
    inside = {"geometry": {"type": "Point", "coordinates": [106.8, -6.2]}}
    outside = {"geometry": {"type": "Point", "coordinates": [110.0, 10.0]}}

    assert is_feature_in_event_scope(event, inside)
    assert not is_feature_in_event_scope(event, outside)


def test_missing_boundary_stops_new_scope_sync(province_cache):
    event = SimpleNamespace(slug="papua-2026", province_codes=["91"])
    with pytest.raises(FileNotFoundError):
        is_feature_in_event_scope(event, {"geometry": {"type": "Point", "coordinates": [136, -4]}})
