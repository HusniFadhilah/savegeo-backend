"""HTTP, permissions, preview/write isolation, and persisted assessment history."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import carbon_field
from app.api.routes.carbon_calibration import router
from app.core.security import get_current_admin_panel
from app.db.base import Base
from app.db.models.admin_user import AdminUser
from app.db.models.audit_log import AuditLog
from app.db.models.carbon_calibration import (
    CarbonCalibrationDataset,
    CarbonCalibrationRun,
    CarbonCalibrationModel,
    CarbonFieldObservation,
)
from app.db.session import get_db


@compiles(JSONB, "sqlite")
def json_sqlite(*args, **kwargs):
    return "JSON"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                AdminUser,
                AuditLog,
                CarbonCalibrationDataset,
                CarbonCalibrationRun,
                CarbonCalibrationModel,
                CarbonFieldObservation,
            )
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(AdminUser(id=1, username="test-admin", password_hash="test"))
        db.add(
            CarbonCalibrationDataset(
                dataset_id="site-one", name="Data uji", manifest={"site": "DATA UJI", "ecosystem": "forest"}
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(router)

    def session():
        with factory() as db:
            yield db

    def principal(request: Request):
        token = request.headers.get("X-Test")
        if not token:
            raise HTTPException(401)
        return SimpleNamespace(
            id=1, username="test-admin", role_id=None if token == "admin" else 1, role=None
        )

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_admin_panel] = principal
    monkeypatch.setattr(carbon_field, "get_settings", lambda: SimpleNamespace(upload_path=tmp_path))
    yield TestClient(app), factory
    engine.dispose()


BASE = "/admin/carbon-calibration/datasets/site-one"


def row(plot="P1", **changes):
    return {
        "plot_id": plot,
        "survey_date": "2025-06-01",
        "pool": "agb",
        "role": "calibration",
        "latitude": -7,
        "longitude": 110,
        "plot_area_m2": 400,
        "reference_type": "field_measurement",
        "method": "Harvest mass",
        "source": "DATA UJI",
        "value": 10,
        **changes,
    }


def test_read_and_write_routes_require_calibration_permission(client):
    http, _ = client
    assert http.get(BASE + "/observations").status_code == 401
    assert http.get(BASE + "/observations", headers={"X-Test": "limited"}).status_code == 403
    assert (
        http.post(
            BASE + "/observations", json={"records": [row()]}, headers={"X-Test": "limited"}
        ).status_code
        == 403
    )


def test_csv_preview_never_writes_and_conflicting_roles_roll_back_the_entire_batch(client):
    http, factory = client
    headers = {"X-Test": "admin"}
    csv = b"plot_id,survey_date,pool,role,latitude,longitude,plot_area_m2,reference_type,method,source,value\nP1,2025-06-01,agb,calibration,-7,110,400,field_measurement,Harvest mass,DATA UJI,10\n"
    assert (
        http.post(BASE + "/observations/csv", files={"file": ("field.csv", csv)}, headers=headers).json()[
            "valid"
        ]
        is True
    )
    assert http.get(BASE + "/observations", headers=headers).json()["total"] == 0
    assert (
        http.post(
            BASE + "/observations/csv?preview=false", files={"file": ("field.csv", csv)}, headers=headers
        ).status_code
        == 200
    )
    response = http.post(
        BASE + "/observations",
        json={"records": [row("P2"), row(survey_date="2024-06-01", role="validation")]},
        headers=headers,
    )
    assert response.status_code == 422
    with factory() as db:
        assert db.query(CarbonFieldObservation).count() == 1
        assert db.query(AuditLog).count() == 1


def test_assessment_artifact_history_and_prediction_immutability(client):
    http, factory = client
    headers = {"X-Test": "admin"}
    records = [
        row(
            f"P{i}",
            value=2 * i + 3,
            prediction={
                "source": "model-v1",
                "year": 2025,
                "value_mg_c_ha": i,
                "scale_m": 30,
                "support": "plot_mean",
            },
        )
        for i in range(1, 5)
    ]
    assert http.post(BASE + "/observations", json={"records": records}, headers=headers).status_code == 200
    items = http.get(BASE + "/observations", headers=headers).json()["rows"]
    summary = http.get(BASE + "/field-summary", headers=headers).json()
    assert summary["pools"]["agb"]["calibration_plots"] == 4
    assert summary["pools"]["agb"]["sources"] == ["model-v1"]
    assert http.get(BASE + "/observations?pool=soc", headers=headers).json()["total"] == 0
    assert http.get(BASE + "/observations?pool=unknown", headers=headers).status_code == 422
    assert (
        http.post(
            BASE + f"/observations/{items[0]['id']}/predictions",
            json={
                "source": "model-v1",
                "year": 2025,
                "value_mg_c_ha": 999,
                "scale_m": 30,
                "support": "plot_mean",
            },
            headers=headers,
        ).status_code
        == 409
    )
    response = http.post(
        BASE + "/field-assessments", json={"pool": "agb", "prediction_source": "model-v1"}, headers=headers
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["metrics"]["artifact"]["slope"] == pytest.approx(2)
    assert len(result["metrics"]["provenance"]["observation_ids"]) == 4
    assert http.get(BASE + "/field-assessments", headers=headers).json()["runs"][0]["id"] == result["id"]
    assert http.get("/admin/carbon-calibration/runs/" + result["id"], headers=headers).json()["metrics"][
        "artifact"
    ]["intercept"] == pytest.approx(3)
    with factory() as db:
        assert db.query(CarbonCalibrationModel).one().is_active is False


def test_overflowing_normalization_is_a_validation_error_not_a_server_error(client):
    http, factory = client
    payload = row(
        pool="deadwood",
        value=None,
        quantity="dry_biomass",
        unit="kg/plot",
        carbon_fraction=0.47,
        deadwood_sample={
            "minimum_diameter_cm": 10,
            "pieces": [
                {
                    "component": "downed",
                    "volume_m3": 1e308,
                    "dry_density_kg_m3": 300,
                    "decay_class": "II",
                    "density_source": "Lab",
                }
            ],
        },
    )
    response = http.post(BASE + "/observations", json={"records": [payload]}, headers={"X-Test": "admin"})
    assert response.status_code == 422
    with factory() as db:
        assert db.query(CarbonFieldObservation).count() == 0
