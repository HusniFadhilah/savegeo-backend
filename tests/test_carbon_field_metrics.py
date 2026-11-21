import pytest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.services.carbon_field_service import _evaluate_validation
from app.services import carbon_field_service, field_prediction_service
from app.schemas.carbon_field import FieldAssessmentRequest
from app.db.base import Base
from app.db.models.carbon_calibration import (
    CarbonCalibrationDataset,
    CarbonCalibrationRun,
    CarbonFieldObservation,
    FieldPlot,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_type, _compiler, **_kwargs):
    return "JSON"


def test_single_independent_plot_reports_preliminary_error_metrics():
    result = _evaluate_validation([{"plot_id": "Bu Dessy 1", "observed": 47.0, "predicted": 51.5}])

    assert result["status"] == "computed"
    assert result["n_plots"] == 1
    assert result["bias"] == 4.5
    assert result["rmse"] == 4.5
    assert result["mae"] == 4.5
    assert result["r2"] is None
    assert result["qualification"] == "single_plot_holdout_not_generalizable"


def test_missing_plot_boundary_uses_confirmed_square_area_proxy(monkeypatch):
    calls = {}

    class _Point:
        def buffer(self, radius, **kwargs):
            calls["radius"] = radius
            return self

        def bounds(self, **kwargs):
            calls["bounds"] = kwargs
            return "square-bounds"

    monkeypatch.setattr(field_prediction_service.ee.Geometry, "Point", staticmethod(lambda coords: _Point()))
    monkeypatch.setattr(field_prediction_service.ee, "Feature", lambda geometry, properties: (geometry, properties))
    observation = type("Observation", (), {
        "id": "observation-1",
        "raw": {"plot_id": "1", "plot_area_m2": 400, "longitude": 110.4, "latitude": -7.0},
    })()

    geometry, properties = field_prediction_service._plot_feature(observation, None)

    assert geometry == "square-bounds"
    assert calls["radius"] == pytest.approx(20 / 2**0.5)
    assert properties["support"] == "north_aligned_square_proxy"


def test_prediction_worker_retry_reuses_plot_mean_and_keeps_run_computed(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        CarbonCalibrationDataset.__table__,
        CarbonCalibrationRun.__table__,
        CarbonFieldObservation.__table__,
        FieldPlot.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)

    class _Collection:
        def geometry(self):
            return self

        def bounds(self, **_kwargs):
            return "roi"

    class _Image:
        def rename(self, _name):
            return self

        def reduceRegions(self, **_kwargs):
            return self

        def getInfo(self):
            return {"features": [{"properties": {
                "observation_id": "bu-dessy-plot-1",
                "prediction_mean": 120.0,
                "support": "plot_mean",
            }}]}

    monkeypatch.setattr(field_prediction_service.ee, "FeatureCollection", lambda _items: _Collection())
    monkeypatch.setattr(
        field_prediction_service.ee, "Reducer", SimpleNamespace(mean=lambda: "mean"), raising=False
    )
    monkeypatch.setattr(field_prediction_service, "_plot_feature", lambda _row, _plot: "plot-feature")
    monkeypatch.setattr(
        "app.services.carbon_saved_models.saved_model_image",
        lambda *_args, **_kwargs: (_Image(), {"map_resolution_m": 10}),
    )
    model = SimpleNamespace(
        id=17,
        name="Approved GEE model",
        version="2.1",
        algorithm="saved_reference_ensemble",
        description="test fixture",
        artifact_sha256="a" * 64,
        approval_digest="b" * 64,
    )
    metadata = {
        "format": "gee_saved_ensemble",
        "method": "saved_reference_ensemble",
        "target_pool": "aboveground_biomass_carbon",
        "reference_year": 2024,
    }
    monkeypatch.setattr(field_prediction_service, "_model_for_run", lambda *_args: (model, metadata))

    with Session(engine) as db:
        dataset = CarbonCalibrationDataset(
            dataset_id="bu-dessy-validation",
            name="Bu Dessy independent plot",
            manifest={"crs": "EPSG:4326"},
            access_classification="restricted",
            status="validated",
        )
        db.add(dataset)
        db.flush()
        observation = CarbonFieldObservation(
            id="bu-dessy-plot-1",
            dataset_pk=dataset.id,
            identity_key="c" * 64,
            sha256="d" * 64,
            pool="agb",
            role="validation",
            observed_mg_c_ha=111.75093,
            raw={
                "plot_id": "1",
                "plot_area_m2": 400,
                "latitude": -7.0527222222,
                "longitude": 110.4396388889,
                "survey_date": "2024-01-01",
                "reference_type": "allometry",
            },
            predictions=[],
        )
        run = CarbonCalibrationRun(
            id="run-bu-dessy-validation",
            dataset_pk=dataset.id,
            status="running",
            method="gee_plot_mean",
            target_pool="aboveground_biomass_carbon",
            parameters={
                "model_id": 17,
                "model_name": model.name,
                "model_version": model.version,
                "artifact_sha256": model.artifact_sha256,
                "approval_digest": model.approval_digest,
                "serving_revision": {"revision": "fixture"},
                "pool": "agb",
                "target_pool": "aboveground_biomass_carbon",
                "analysis_year": 2024,
                "observation_ids": [observation.id],
            },
            worker_id="worker-first",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.add_all([observation, run])
        db.commit()

        field_prediction_service.run_field_prediction(db, run.id, "worker-first")
        db.refresh(run)
        db.refresh(observation)
        assert run.status == "complete"
        assert run.metrics["predictions_saved"] == 1
        assert run.metrics["predictions_reused"] == 0
        assert observation.predictions[0]["support"] == "plot_mean"
        assert observation.predictions[0]["value_mg_c_ha"] == 120.0

        run.status = "running"
        run.worker_id = "worker-retry"
        run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        field_prediction_service.run_field_prediction(db, run.id, "worker-retry")
        db.refresh(run)
        db.refresh(observation)
        assert run.status == "complete"
        assert run.metrics["status"] == "computed"
        assert run.metrics["predictions_saved"] == 0
        assert run.metrics["predictions_reused"] == 1
        assert len(observation.predictions) == 1

        assessment = carbon_field_service.assess(
            [observation.to_dict()],
            FieldAssessmentRequest(
                pool="agb",
                prediction_source="gee:Approved GEE model@2.1:2024:aaaaaaaa",
                create_calibration_candidate=False,
            ),
        )
        validation = assessment["baseline_validation"]
        assert assessment["status"] == "computed"
        assert validation["n_plots"] == 1
        assert validation["bias"] == pytest.approx(8.24907)
        assert validation["rmse"] == pytest.approx(8.24907)
        assert validation["mae"] == pytest.approx(8.24907)
        assert validation["qualification"] == "single_plot_holdout_not_generalizable"

    Base.metadata.drop_all(engine, tables=tables)
    engine.dispose()
