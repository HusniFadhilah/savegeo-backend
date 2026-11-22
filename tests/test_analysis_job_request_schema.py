import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.security import get_current_app_viewer
from app.db.session import get_db
from app.main import app
from app.api.routes import analysis_jobs
from app.schemas.analysis_jobs import AnalysisJobRequest


def test_analysis_job_payload_accepts_module_specific_fields_with_typed_shared_inputs():
    request = AnalysisJobRequest.model_validate({
        "type": "carbon",
        "payload": {
            "year": 2024,
            "cloud_threshold": 25,
            "model_name": "custom-model-v2",
            "aoi": {"region": {"level": "city", "codes": ["3374"]}},
        },
    })
    assert request.payload.year == 2024
    assert request.payload.model_extra["model_name"] == "custom-model-v2"


@pytest.mark.parametrize("payload", [
    {"cloud_threshold": 100.1},
    {"scale": 0},
    {"year": 2201},
    {"from_year": "not-a-year"},
    {"limit": 250_001},
    {"__proto__": {"is_admin": True}},
    [],
])
def test_analysis_job_payload_rejects_out_of_range_or_non_object_inputs(payload):
    with pytest.raises(ValidationError):
        AnalysisJobRequest.model_validate({"type": "carbon", "payload": payload})


def test_analysis_job_payload_rejects_non_finite_numbers():
    with pytest.raises(ValidationError):
        AnalysisJobRequest.model_validate({"type": "vegetation", "payload": {"scale": float("inf")}})


def test_preflight_block_uses_standard_problem_status_and_error_code(monkeypatch):
    monkeypatch.setattr(analysis_jobs, "ensure_job_feature_access", lambda *_args: None)
    app.dependency_overrides[get_current_app_viewer] = lambda: object()
    app.dependency_overrides[get_db] = lambda: object()
    try:
        with TestClient(app) as client:
            response = client.post("/api/analysis-jobs/preflight", json={
                "type": "carbon",
                "payload": {"aoi": {"west": 181, "south": -7, "east": 182, "north": -6}},
            })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 422
    assert response.json()["code"] == "invalid_aoi"
    assert response.json()["preflight"]["status"] == "blocked"


def test_framework_404_uses_the_same_problem_error_contract():
    with TestClient(app) as client:
        response = client.get("/api/route-that-does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["detail"] == response.json()["error"]
