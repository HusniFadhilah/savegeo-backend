from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.disaster_completeness_service import build_completeness
from app.api.routes import disaster_events


def test_landcover_and_exposure_never_complete_damage_assessment():
    entries = [
        {"model_id": "dynamic_world_v1", "available": True, "result_semantics": "land_cover", "damage_model": False,
         "result": {"comparison": {"pre": {}, "post": {}, "transitions": []}}},
        {"model_id": "flood_building_exposure_v1", "available": True, "result_semantics": "building_exposure", "damage_model": False},
    ]
    report = build_completeness("flood", entries)
    sections = {s["id"]: s for s in report["sections"]}
    assert sections["land_change"]["status"] == "available"
    assert sections["trees"]["status"] == "available"
    assert sections["water"]["status"] == "available"
    assert sections["buildings"]["status"] == "available"
    assert sections["building_damage"]["status"] != "available"
    assert not report["complete"]


def test_unpublished_and_post_only_results_do_not_establish_change():
    report = build_completeness("earthquake", [
        {"available": False, "result_semantics": "land_cover"},
        {"model_id": "dynamic_world_v1", "available": True, "result_semantics": "land_cover", "result": {}},
    ])
    assert next(s for s in report["sections"] if s["id"] == "land_change")["status"] != "available"


def test_event_segmentation_rejects_another_events_raster(monkeypatch):
    monkeypatch.setattr(disaster_events, "_get_published_event", lambda *a: object())
    monkeypatch.setattr(disaster_events.disaster_repo, "get_imagery", lambda *a: SimpleNamespace(event_id=2))
    with pytest.raises(HTTPException) as error:
        disaster_events.start_event_segmentation(1, disaster_events.EventSegmentationRequest(imagery_id=3), SimpleNamespace(id=4), None)
    assert error.value.status_code == 404


def test_event_segmentation_uses_only_server_registered_raster(monkeypatch):
    monkeypatch.setattr(disaster_events, "_get_published_event", lambda *a: object())
    image = SimpleNamespace(id=3, event_id=1, source_kind="local_upload", local_file_path="var/real.tif", resolution_m=1,
                            scene_id="blacksky-real", acquisition_date=__import__("datetime").date(2026, 8, 16))
    monkeypatch.setattr(disaster_events.disaster_repo, "get_imagery", lambda *a: image)
    monkeypatch.setattr(disaster_events.disaster_repo, "get_active_aoi", lambda *a: SimpleNamespace(geojson={
        "type": "Polygon", "coordinates": [[[119, -8], [119.1, -8], [119.1, -8.1], [119, -8.1], [119, -8]]] }))
    captured = {}
    def start(data, owner):
        captured.update(data=data, owner=owner)
        return {"job_id": "example", "status": "running"}
    monkeypatch.setattr(disaster_events.samgeo_service, "start_job", start)
    disaster_events.start_event_segmentation(1, disaster_events.EventSegmentationRequest(imagery_id=3, mode="automatic"), SimpleNamespace(id=4), None)
    assert captured["data"]["local_file_path"] == "var/real.tif"
    assert captured["data"]["class_label"] is None  # SAM automatic has no semantic building class.
    assert not captured["data"].get("cleanup_local_file", False)
    assert captured["owner"].endswith(":4")


def test_segmentation_owners_are_separate_per_user_and_event():
    assert disaster_events._segmentation_owner(1, SimpleNamespace(id=4)) != disaster_events._segmentation_owner(2, SimpleNamespace(id=4))
    assert disaster_events._segmentation_owner(1, SimpleNamespace(id=4)) != disaster_events._segmentation_owner(1, SimpleNamespace(id=5))
