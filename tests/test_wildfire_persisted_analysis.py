from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app.services import wildfire_analysis_service as wildfire


def _event():
    return SimpleNamespace(
        id=40,
        disaster_type="forest_fire",
        monitoring_from=date(2026, 1, 1),
        monitoring_to=date(2026, 1, 3),
        start_date=None,
        end_date=None,
        bbox=[110, -2, 111, -1],
    )


def _aoi():
    return SimpleNamespace(id=9, event_id=40, geojson={"type": "Polygon"})


def test_hotspot_product_persists_feature_collection_without_area():
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [110.2, -1.2]}}]
    with (
        patch.object(wildfire.wildfire_hotspot_service, "sync_event_hotspots", return_value={"stored": 1}),
        patch.object(wildfire.wildfire_hotspot_service, "list_event_features", return_value=features),
        patch.object(wildfire.wildfire_hotspot_service, "summarize", return_value={"total": 1}),
    ):
        output = wildfire.compute_persisted_product(
            None, _event(), _aoi(), None, None, "fire_hotspot_observation_v1", {}
        )
    assert output["tile_url"] is None
    assert output["statistics"]["area_ha"] is None
    assert output["features"]["features"] == features


def test_burned_area_product_keeps_source_products_separate():
    loaded = {
        "sources": [
            {"id": "mcd64a1", "status": "ok", "kind": "burned_area", "tile_url": "tile", "area_ha": 12.5},
            {"id": "vnp64a1", "status": "no_data"},
        ]
    }
    with patch.object(wildfire.fire_multi_source_service, "load_sources", return_value=loaded) as load:
        output = wildfire.compute_persisted_product(
            None, _event(), _aoi(), None, None, "fire_burned_area_v1", {"sources": ["mcd64a1"]}
        )
    assert output["tile_url"] == "tile"
    assert output["statistics"]["area_ha"] == 12.5
    load.assert_called_once()


def test_dnbr_and_sam_outputs_are_reviewable_products():
    pre = SimpleNamespace(acquisition_date=date(2026, 1, 1))
    post = SimpleNamespace(acquisition_date=date(2026, 1, 3))
    mapped = {"tile_url": "dnbr-tile", "area_ha": 4, "legend": [], "hotspots": {"type": "FeatureCollection", "features": []}}
    with patch.object(wildfire.disaster_service, "get_disaster_event_map", return_value=mapped):
        dnbr = wildfire.compute_persisted_product(None, _event(), _aoi(), pre, post, "fire_dnbr_v1", {})
    assert dnbr["tile_url"] == "dnbr-tile"
    assert dnbr["statistics"]["area_ha"] == 4

    with patch.object(
        wildfire.samgeo_service,
        "get_job",
        return_value={"status": "complete", "result": {"type": "FeatureCollection", "features": []}},
    ):
        sam = wildfire.compute_persisted_product(None, _event(), _aoi(), None, None, "fire_sam_candidate_v1", {"job_id": "job"})
    assert sam["requires_review"] is True
    assert sam["statistics"]["candidate_count"] == 0
