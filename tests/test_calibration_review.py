import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import carbon_calibration as routes
from app.db.models.carbon_calibration import CarbonCalibrationDataset, CarbonCalibrationModel, CarbonCalibrationRun, FieldPlot
from app.services import calibration_review as service

pytest_plugins = ["tests.test_organizations"]
POOL = "aboveground_biomass_carbon"


def seed(factory, tmp_path, count=1):
    with factory() as db:
        domain = {"type": "Polygon", "coordinates": [[[106.0, -6.0], [107.0, -6.0], [107.0, -5.0], [106.0, -5.0], [106.0, -6.0]]]}
        db.add(CarbonCalibrationDataset(id=1, dataset_id="field-pilot", name="Survey", manifest={"site": "test", "application_domain_geojson": domain}, validation_report={}))
        db.flush()
        for plot_id in ("p1", "p2", "p3", "p4", "p5", "p6"):
            db.add(FieldPlot(dataset_pk=1, plot_id=plot_id, crs="EPSG:4326", attributes={}, qa_status="valid"))
        for ident in range(1, count + 1):
            path = tmp_path / f"calibration-{ident}.json"
            content = b'{"method":"fixture-only","slope":1}'
            path.write_bytes(content)
            parameters = {
                "baseline_model": {
                    "id": "carbon-base-v1", "version": "1", "artifact_sha256": "a" * 64,
                    "preprocessing_version": "sentinel2-v1", "sensors": ["sentinel-2"],
                },
                "training_plot_ids": ["p1", "p2", "p3"],
                "validation_plot_ids": ["p4", "p5", "p6"],
            }
            metrics = {
                "status": "computed", "sample_count": 6, "validation_type": "independent_field_plot_holdout",
                "independent_validation_metrics": {"status": "computed", "sample_count": 3, "rmse": 1.2},
            }
            db.add(CarbonCalibrationRun(id=f"run-{ident}", dataset_pk=1, status="complete", method="linear_calibration",
                                        target_pool=POOL, parameters=parameters, metrics=metrics, created_by=1))
            db.flush()
            db.add(CarbonCalibrationModel(id=ident, model_id=f"model-{ident}", dataset_pk=1, run_id=f"run-{ident}",
                                         model_name="Local fixture", version="1", target_pool=POOL, status="experimental",
                                         artifact_path=str(path), artifact_sha256=hashlib.sha256(content).hexdigest(), metadata_json={}, is_active=False))
        db.commit()


def test_approve_alias_cannot_skip_promotion_guard(env, tmp_path):
    _, factory, _ = env
    seed(factory, tmp_path)
    with factory() as db:
        run = db.get(CarbonCalibrationRun, "run-1")
        run.metrics = {"status": "not_computed", "sample_count": 1}
        db.commit()
        for approve in (lambda: routes.approve_run("run-1", SimpleNamespace(id=2), db),
                        lambda: routes.promote_model(1, SimpleNamespace(id=2), db)):
            with pytest.raises(HTTPException) as error:
                approve()
            assert "minimum_plot_count_not_met" in error.value.detail["blockers"]
            db.rollback()
        assert db.get(CarbonCalibrationModel, 1).status == "experimental"


def test_review_separation_and_changed_inputs_invalidate_activation(env, tmp_path):
    _, factory, _ = env
    seed(factory, tmp_path)
    with factory() as db:
        with pytest.raises(HTTPException) as error:
            service.approve(db, 1, 1)
        assert error.value.detail["code"] == "separation_of_duties"
        db.rollback()
        model = service.approve(db, 1, 2)
        assert model.approved_by == 2 and model.status == "approved_local"
        assert service.activate(db, 1, 1).is_active
        dataset = db.get(CarbonCalibrationDataset, 1)
        dataset.validation_report = {"review_note": "new input"}
        db.commit()
        with pytest.raises(HTTPException) as error:
            service.activate(db, 1, 1)
        assert error.value.detail["code"] == "approval_required"


def test_rollback_restores_previous_approved_model_and_keeps_activation_history(env, tmp_path):
    _, factory, _ = env
    seed(factory, tmp_path, 2)
    with factory() as db:
        service.approve(db, 1, 2, reason="Reviewed plot holdout evidence", reviewer={"username": "reviewer", "role": "geospatial_expert"})
        service.approve(db, 2, 2, reason="Reviewed updated calibration", reviewer={"username": "reviewer", "role": "geospatial_expert"})
        service.activate(db, 1, 1)
        service.activate(db, 2, 1)

        rolled_back, restored = service.rollback(db, 2, 1, reason="Regression check failed")

        assert rolled_back.status == "rolled_back" and not rolled_back.is_active
        assert restored.id == 1 and restored.status == "active_local" and restored.is_active
        assert rolled_back.metadata_json["rollback_reason"] == "Regression check failed"
        assert restored.metadata_json["activation_history"][-1]["event"] == "rollback_restored"


def test_rejection_requires_independent_reviewer_and_reason(env, tmp_path):
    _, factory, _ = env
    seed(factory, tmp_path)
    with factory() as db:
        with pytest.raises(HTTPException) as error:
            service.reject(db, 1, 1, reason="insufficient independent plots")
        assert error.value.detail["code"] == "separation_of_duties"
        db.rollback()

        rejected = service.reject(db, 1, 2, reason="insufficient independent plots", reviewer={"username": "reviewer"})
        assert rejected.status == "rejected" and not rejected.is_active
        assert rejected.metadata_json["review"]["decision"] == "rejected"
        assert rejected.metadata_json["review"]["reason"] == "insufficient independent plots"


def test_changed_artifact_cannot_be_approved_or_activated(env, tmp_path):
    _, factory, _ = env
    seed(factory, tmp_path)
    with factory() as db:
        service.approve(db, 1, 2)
        (tmp_path / "calibration-1.json").write_bytes(b"changed")
        for operation in (lambda: service.approve(db, 1, 2), lambda: service.activate(db, 1, 1)):
            with pytest.raises(HTTPException) as error:
                operation()
            assert "artifact_checksum_mismatch" in error.value.detail["blockers"]
            db.rollback()


def test_concurrent_activation_keeps_one_model_active_per_pool(env, tmp_path):
    _, factory, dialect = env
    if dialect != "postgresql":
        pytest.skip("Row-lock concurrency requires PostgreSQL")
    seed(factory, tmp_path, 2)
    with factory() as db:
        service.approve(db, 1, 2)
        service.approve(db, 2, 2)
    barrier = threading.Barrier(2)
    def activate(ident):
        with factory() as db:
            barrier.wait(timeout=10)
            service.activate(db, ident, 1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(activate, ident) for ident in (1, 2)]
        for future in futures:
            future.result(timeout=20)
    with factory() as db:
        assert len(list(db.scalars(select(CarbonCalibrationModel).where(CarbonCalibrationModel.is_active.is_(True))))) == 1
