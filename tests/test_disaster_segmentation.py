import datetime as dt
from types import SimpleNamespace as Row
from unittest.mock import MagicMock, patch

import pytest

from app.registries.disaster_model_registry import get_model, list_models
from app.services import disaster_analysis_service as service
from app.services.disaster_segmentation_service import class_statistics
from app.services.gee_common import AnalysisError


def inputs(disaster_type="flood"):
    return (Row(disaster_type=disaster_type), Row(event_id=1, geojson={"type": "Polygon", "coordinates": []}),
        {10: Row(event_id=1, phase="pre", source_kind="gee", satellite="Sentinel-2", resolution_m=10, acquisition_date=dt.date(2024,1,1)),
         11: Row(event_id=1, phase="post", source_kind="gee", satellite="Sentinel-2", resolution_m=10, acquisition_date=dt.date(2024,2,1))})


@pytest.mark.parametrize("kind", ["flood", "landslide", "forest_fire", "tsunami", "earthquake"])
def test_supported_event_uses_real_multiclass_schema(kind):
    models = list_models(True, kind)
    assert models[0]["model_id"] == "dynamic_world_v1"
    assert len(models[0]["classes"]) == 9
    assert models[0]["classes"][0]["label"] == "Water"
    event, aoi, images = inputs(kind)
    with patch.object(service.disaster_repo, "get_event", return_value=event), patch.object(service.disaster_repo, "get_aoi", return_value=aoi), patch.object(service.disaster_repo, "get_imagery", side_effect=lambda db, key: images.get(key)):
        assert service.validate_inputs(None, 1, 1, 10, 11, "dynamic_world_v1")[0]["multiclass"]


@pytest.mark.parametrize("failure", ["missing_pre", "missing_post", "foreign_event", "wrong_phase", "foreign_aoi", "local_raster", "wrong_sensor", "wrong_resolution", "date_order", "disabled"])
def test_rejects_invalid_input_without_touching_gee(failure):
    event, aoi, images = inputs()
    pre, post, model = 10, 11, "dynamic_world_v1"
    if failure == "missing_pre": pre = None
    if failure == "missing_post": post = None
    if failure == "foreign_event": images[11].event_id = 2
    if failure == "wrong_phase": images[11].phase = "pre"
    if failure == "foreign_aoi": aoi.event_id = 2
    if failure == "local_raster": images[11].source_kind = "local_upload"
    if failure == "wrong_sensor": images[11].satellite = "Landsat"
    if failure == "wrong_resolution": images[11].resolution_m = 20
    if failure == "date_order": images[11].acquisition_date = images[10].acquisition_date
    if failure == "disabled": model = "building_segmentation_v1"
    with patch.object(service.disaster_repo, "get_event", return_value=event), patch.object(service.disaster_repo, "get_aoi", return_value=aoi), patch.object(service.disaster_repo, "get_imagery", side_effect=lambda db, key: images.get(key)):
        with pytest.raises(AnalysisError): service.validate_inputs(None, 1, 1, pre, post, model)


def test_class_zero_kept_nodata_not_fabricated_and_confidence_nullable():
    schema = get_model("dynamic_world_v1")["classes"]
    rows = class_statistics(schema, [{"class_id": 0, "sum": [20, 2000, 1600]}], [{"class_id": 1, "sum": [20, 2000, 1200]}], 100)
    assert rows[0]["pre_percentage"] == 20
    assert rows[0]["pre_confidence"] == .8
    assert rows[0]["post_confidence"] is None
    assert rows[1]["change_area_ha"] == 20
    assert sum(r["pre_percentage"] for r in rows) == 20


@pytest.mark.parametrize("groups", [[], [{"class_id": 99, "sum": [1,1,1]}], [{"class_id": 0, "sum": [101,1,1]}], [{"class_id": 0, "sum": [float('nan'),1,1]}]])
def test_invalid_or_empty_raster_never_succeeds(groups):
    with pytest.raises(AnalysisError): class_statistics(get_model("dynamic_world_v1")["classes"], groups, groups, 100)


def test_failure_is_persisted_and_logged_before_result_creation():
    db = MagicMock()
    run = Row(id=5, event_id=1, aoi_id=1, pre_imagery_id=10, post_imagery_id=11, model_id="dynamic_world_v1", status="queued")
    with patch.object(service.disaster_repo, "get_run", return_value=run), patch.object(service, "validate_inputs", side_effect=AnalysisError("No pre imagery", 400)), patch.object(service.disaster_repo, "upsert_result") as save:
        with pytest.raises(AnalysisError): service.run_analysis(db, 5)
    assert run.status == "failed"
    assert run.error_message == "No pre imagery"
    save.assert_not_called()


def test_processing_run_cannot_start_twice():
    with patch.object(service.disaster_repo, "get_run", return_value=Row(status="processing")):
        with pytest.raises(AnalysisError, match="masih diproses"): service.run_analysis(MagicMock(), 1)


def test_success_saved_then_cached_without_second_inference():
    from app.services import disaster_segmentation_service as segmentation
    event, aoi, images = inputs()
    model = get_model("dynamic_world_v1")
    run = Row(id=5,event_id=1,aoi_id=1,pre_imagery_id=10,post_imagery_id=11,model_id=model["model_id"],status="queued",completed_at=None)
    run.to_dict = lambda: {"status":run.status,"model_version":run.model_version}
    stored = []
    def save(db, run_id, output):
        result = Row(statistics=output["statistics"])
        result.to_dict = lambda **kwargs: {"tile_url":output["tile_url"],"statistics":result.statistics}
        stored[:] = [result]
        return result
    with patch.object(service.disaster_repo,"get_run",return_value=run), patch.object(service,"validate_inputs",return_value=(model,aoi,images[10],images[11])), patch.object(service,"create_geometry_from_payload"), patch.object(service.disaster_repo,"list_runs_for_event",return_value=[run]), patch.object(service.disaster_repo,"get_result_for_run",side_effect=lambda *args: stored[0] if stored else None), patch.object(service.disaster_repo,"upsert_result",side_effect=save), patch.object(segmentation,"compute_segmentation",return_value={"tile_url":"real-output-contract","statistics":{"comparison":{"review_reasons":["low coverage"]}}}) as compute:
        first = service.run_analysis(MagicMock(),5)
        second = service.run_analysis(MagicMock(),5)
    assert first["run"]["status"] == "review_required"
    assert second["cached"] is True
    compute.assert_called_once()
