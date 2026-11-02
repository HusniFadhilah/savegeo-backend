from unittest.mock import MagicMock

import pytest

from app.api.routes import analysis_jobs
from app.services.aoi_reference import resolve_region_aoi


@pytest.mark.parametrize("kind", ["vegetation", "landcover", "landcover_change", "landcover_transition", "landcover_change_map", "landcover_hotspots", "landcover_fusion"])
def test_analysis_worker_resolves_exact_boundary_before_computing(monkeypatch, kind):
    geometry = {"type": "Polygon", "coordinates": [
        [[95, 3], [97, 3], [97, 5], [95, 3]],
        [[95.1, 3.1], [95.2, 3.1], [95.2, 3.2], [95.1, 3.1]],
    ]}
    payload = {"aoi": {"region": {"level": "city", "codes": ["1105"]}}, "year": 2026, "indices": ["NDVI"]}
    load_boundary = MagicMock(return_value=geometry)
    monkeypatch.setattr(analysis_jobs, "resolve_known_aoi", lambda value: resolve_region_aoi(value, load_boundary))
    db = MagicMock()
    monkeypatch.setattr(analysis_jobs, "SessionLocal", lambda: db)
    states = []
    monkeypatch.setattr(analysis_jobs, "_write_job", lambda _id, state: states.append(state))
    monkeypatch.setattr(analysis_jobs, "_read_job", lambda _id: {"started_at": "2026-11-02T00:00:00Z"})
    monkeypatch.setattr(analysis_jobs.provenance_service, "update_analysis_provenance", MagicMock())
    monkeypatch.setattr(analysis_jobs.output_registry_service, "register_action_outputs", MagicMock())
    service = MagicMock(return_value={"tile_url": "/test-tile", "statistics": {"mean": 0.5}})
    if kind == "vegetation":
        monkeypatch.setattr(analysis_jobs.vegetation_service, "analyze_vegetation", service)
    elif kind == "landcover_fusion":
        monkeypatch.setattr(analysis_jobs.landcover_fusion_service, "analyze_landcover_fusion", service)
    elif kind in {"landcover_transition", "landcover_change_map", "landcover_hotspots"}:
        monkeypatch.setattr(analysis_jobs.landcover_service, f"analyze_{kind}", service)
    else:
        monkeypatch.setattr(analysis_jobs.landcover_service, "analyze_landcover", service)
    analysis_jobs._run_job("test-region-job", kind, payload)
    actual = service.call_args.args[-1]
    assert actual["aoi"]["geojson"]["geometry"] == geometry
    assert actual["year"] == 2026 and actual["indices"] == ["NDVI"]
    assert payload["aoi"] == {"region": {"level": "city", "codes": ["1105"]}}
    load_boundary.assert_called_once_with("city", "1105")
    assert states[-1]["status"] == "succeeded"
    db.close.assert_called_once()
