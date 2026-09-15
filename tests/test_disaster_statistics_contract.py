from types import SimpleNamespace

from app.api.routes.disaster_events import _compatible_model_ids


def test_statistics_model_set_matches_event_type():
    flood = _compatible_model_ids(SimpleNamespace(disaster_type="flood"))
    assert "flood_change_v1" in flood
    assert "water_segmentation_v1" in flood
    assert "forest_change_v1" not in flood

    fire = _compatible_model_ids(SimpleNamespace(disaster_type="forest_fire"))
    assert "forest_change_v1" in fire
    assert "flood_change_v1" not in fire


def test_unknown_event_type_has_no_compatible_models():
    assert _compatible_model_ids(SimpleNamespace(disaster_type="unknown")) == set()
