import pytest
from pydantic import ValidationError

from app.api.routes.download import RasterTaskStatusRequest
from app.api.routes.landcover_models import LandcoverModelConfigPayload, LandcoverTrainingRequest
from app.api.routes.wildfire_events import CamsAggregateRequest
from app.schemas.imagery_analysis import ImageryAnalysisRequest


def test_landcover_training_schema_rejects_unknown_model_and_asset_injection():
    with pytest.raises(ValidationError):
        LandcoverTrainingRequest.model_validate({
            "model": "unknown",
            "asset_id": "projects/savegeo/assets/model",
            "model_version": "v1",
        })
    with pytest.raises(ValidationError):
        LandcoverTrainingRequest.model_validate({
            "model": "AlphaEarth_RF",
            "asset_id": "projects/savegeo/assets/../secrets",
            "model_version": "v1",
        })


def test_landcover_config_schema_requires_bounded_object_entries():
    parsed = LandcoverModelConfigPayload.model_validate({"Dynamic_World": {"enabled": True}})
    assert parsed.root["Dynamic_World"]["enabled"] is True
    with pytest.raises(ValidationError):
        LandcoverModelConfigPayload.model_validate({"bad": "free text"})


def test_cams_aggregate_schema_bounds_bbox_and_requires_spatial_input():
    assert CamsAggregateRequest.model_validate({"mode": "area", "bbox": [100, -5, 101, -4]}).mode == "area"
    with pytest.raises(ValidationError):
        CamsAggregateRequest.model_validate({"mode": "grid"})
    with pytest.raises(ValidationError):
        CamsAggregateRequest.model_validate({"mode": "area", "bbox": [181, -5, 182, -4]})


def test_raster_task_status_token_is_bounded_and_control_free():
    assert RasterTaskStatusRequest.model_validate({"task_token": "task:abc123"}).task_token == "task:abc123"
    with pytest.raises(ValidationError):
        RasterTaskStatusRequest.model_validate({"task_token": "task\nsecret"})


def test_imagery_request_schema_bounds_common_values_and_urls():
    parsed = ImageryAnalysisRequest.model_validate({
        "satellite": "sentinel2",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "limit": 250,
        "longitude": 110.4,
        "latitude": -7.0,
        "item_url": "https://catalog.example/item/1",
        "extension_field": "kept for provider compatibility",
    })
    assert parsed.limit == 250
    assert parsed.model_extra["extension_field"].startswith("kept")
    with pytest.raises(ValidationError):
        ImageryAnalysisRequest.model_validate({"limit": 100_000})
    with pytest.raises(ValidationError):
        ImageryAnalysisRequest.model_validate({"item_url": "http://127.0.0.1/private"})
    with pytest.raises(ValidationError):
        ImageryAnalysisRequest.model_validate({"start_date": "2026-12-31", "end_date": "2026-01-01"})
