from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models.crop_monitoring_cache import CropMonitoringCache
from app.services import crop_monitoring_service as service, crop_result_cache as cache


def field():
    return SimpleNamespace(id=3, area_ha=38606, planting_date=None, geojson={"type": "Polygon"},
                           to_dict=lambda **kw: {"id": 3, "updated_at": "2026-11-02", "geojson": {"coordinates": [1]}})


def test_cache_identity_separates_geometry_period_and_real_section_dependencies():
    f = field()
    params = {"start_date": "2026-08-01", "end_date": "2026-11-02", "scale": 40, "timeseries_index": "NDVI"}
    key = cache.section_key(f, params, "health")
    assert key == cache.section_key(f, {**params, "timeseries_index": "EVI", "data_sources": {"sentinel1": False}}, "health")
    assert key != cache.section_key(f, {**params, "end_date": "2025-11-02"}, "health")
    assert cache.section_key(f, params, "timeseries") != cache.section_key(f, {**params, "timeseries_index": "EVI"}, "timeseries")
    f.to_dict = lambda **kw: {"id": 3, "geojson": {"coordinates": [2]}}
    assert key != cache.section_key(f, params, "health")


def test_cache_roundtrip_survives_sessions_and_expires_temporary_map_ids():
    @compiles(JSONB, "sqlite")
    def jsonb_sqlite(type_, compiler, **kwargs):
        return "JSON"

    engine = create_engine("sqlite://")
    CropMonitoringCache.__table__.create(engine)
    result = {"available": True, "tile_url": "temporary-map", "ndvi_mean": .4}
    with Session(engine) as db:
        cache.save(db, "key", 3, "health", result)
    with Session(engine) as db:
        loaded = cache.load(db, "key")
        assert loaded == result
        loaded["ndvi_mean"] = .9
        assert cache.load(db, "key")["ndvi_mean"] == .4
        db.get(CropMonitoringCache, "key").expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    with Session(engine) as db:
        assert cache.load(db, "key") is None


def test_one_section_failure_keeps_success_and_does_not_compute_weather(monkeypatch):
    f = field()
    monkeypatch.setattr(service.field_repo, "get_field", lambda *_: f)
    monkeypatch.setattr(service, "create_geometry_from_payload", lambda *_: "aoi")
    monkeypatch.setattr(service.config_service, "get_analysis_defaults", lambda *_: {"cloud_threshold": 10, "veg_scale": 10})
    monkeypatch.setattr(cache, "load", lambda *_: None)
    saved = MagicMock()
    monkeypatch.setattr(cache, "save", saved)
    monkeypatch.setattr(service, "_landcover", MagicMock(side_effect=RuntimeError("upstream failed")))
    monkeypatch.setattr(service, "_crop_health", lambda *_: {"available": True, "ndvi_mean": .45})
    weather = MagicMock()
    monkeypatch.setattr(service.weather_service, "get_weather_stats", weather)
    result = service.run_crop_monitoring(MagicMock(), {"field_id": 3, "sub_analyses": ["landcover", "health"]}, "user:1")
    assert result["sub_analyses"]["health"]["ndvi_mean"] == .45
    assert result["sub_analyses"]["landcover"]["available"] is False
    assert result["skipped"][0]["sub_analysis"] == "landcover"
    assert saved.call_count == 1
    weather.assert_not_called()
    assert service._COMPOSITES.get() is None


def test_composite_reused_only_within_a_request(monkeypatch):
    compute = MagicMock(return_value=("image",))
    monkeypatch.setattr(service.vegetation_service, "_composite_for_period", compute)
    args = (None, "aoi", 2026, 8, 11, 10, "sentinel2", "scl")
    token = service._COMPOSITES.set({})
    try:
        service._composite_for_period(*args)
        service._composite_for_period(*args)
        assert compute.call_count == 1
    finally:
        service._COMPOSITES.reset(token)
    service._composite_for_period(*args)
    assert compute.call_count == 2


def test_regional_summary_bounds_work_without_coarsening_small_fields():
    assert service.crop_summary_scale(50, 10) == 10
    assert service.crop_summary_scale(38606, 10) == 40
    scale = service.crop_summary_scale(187920316.68, 10)
    assert 187920316.68 * 10000 / scale ** 2 <= 250000
    with pytest.raises(service.AnalysisError):
        service.crop_summary_scale(0, 10)
