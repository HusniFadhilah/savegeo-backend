import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services.wildfire_geometry import is_feature_in_event_scope, is_sumatra_geometry


@pytest.mark.parametrize("lon,lat", [(101.45, 0.51), (103.6, -1.61), (104.75, -2.99), (98.65, 3.59), (105.27, -5.4), (106.1, -2.1)])
def test_sumatra_mask_keeps_sumatra_and_offshore_provinces(lon, lat):
    assert is_sumatra_geometry({"type": "Point", "coordinates": [lon, lat]})


@pytest.mark.parametrize("lon,lat", [(101.68, 3.14), (103.82, 1.35), (106.85, -6.2), (98, -4.5), (113, -1)])
def test_sumatra_mask_excludes_malaysia_singapore_java_and_ocean(lon, lat):
    feature = {"geometry": {"type": "Point", "coordinates": [lon, lat]}}
    assert not is_feature_in_event_scope(SimpleNamespace(slug="sumatra-2026"), feature)


def test_kalimantan_and_sumatra_masks_remain_independent():
    sumatra = {"geometry": {"type": "Point", "coordinates": [101.45, 0.51]}}
    kalimantan = {"geometry": {"type": "Point", "coordinates": [113, -1]}}
    assert is_feature_in_event_scope(SimpleNamespace(slug="sumatra-2026"), sumatra)
    assert not is_feature_in_event_scope(SimpleNamespace(slug="kalimantan-2026"), sumatra)
    assert is_feature_in_event_scope(SimpleNamespace(slug="kalimantan-2026"), kalimantan)
    assert not is_feature_in_event_scope(SimpleNamespace(slug="sumatra-2026"), kalimantan)


def migration():
    file = Path(__file__).resolve().parents[1] / "alembic/versions/0030_seed_sumatra_wildfire_event.py"
    spec = importlib.util.spec_from_file_location("sumatra_seed", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_configures_ten_provinces_without_fabricating_observations(monkeypatch):
    seed = migration()
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = None
    monkeypatch.setattr(seed.op, "get_bind", lambda: db)
    seed.upgrade()
    parameters = db.execute.call_args_list[-1].args[0].compile(dialect=postgresql.dialect()).params
    assert parameters["slug"] == "sumatra-2026"
    assert parameters["status"] == "published"
    assert len(parameters["province_codes"]) == len(parameters["province"]) == 10
    assert set(parameters["province_codes"]) == {"11", "12", "13", "14", "15", "16", "17", "18", "19", "21"}
    assert parameters["bbox"] == [94.9, -6.1, 109.3, 6.1]
    assert "last_data_at" not in parameters
    assert "last_synced_at" not in parameters
    assert "hotspot_last_synced_at" not in parameters


def test_existing_event_keeps_publication_decision_and_data(monkeypatch):
    seed = migration()
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = 42
    monkeypatch.setattr(seed.op, "get_bind", lambda: db)
    seed.upgrade()
    parameters = db.execute.call_args_list[-1].args[0].compile(dialect=postgresql.dialect()).params
    assert len(parameters["province_codes"]) == 10
    assert "status" not in parameters
    assert "last_data_at" not in parameters
