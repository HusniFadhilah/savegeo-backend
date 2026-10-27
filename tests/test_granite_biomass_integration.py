"""The optional Granite model stays isolated and undiscoverable until configured."""
from pathlib import Path
import pytest

from app.api.routes import models as model_routes
from app.core.config import Settings
from app.registries import granite_biomass_registry as registry
from app.services import carbon_service
from app.services import granite_biomass_service
from app.services.gee_common import AnalysisError


class Query:
    def filter_by(self, **_kwargs):
        return self

    def all(self):
        return []


class EmptyDB:
    def query(self, *_args):
        return Query()


def test_granite_model_is_hidden_until_all_runtime_inputs_are_configured(tmp_path, monkeypatch):
    assert registry.granite_biomass_model(Settings(ibm_granite_biomass_enabled=False)) is None

    checkpoint = tmp_path / "biomass_model.ckpt"
    config = tmp_path / "config.yaml"
    checkpoint.write_bytes(b"test checkpoint")
    config.write_text("model: {}\n", encoding="utf-8")
    settings = Settings(
        ibm_granite_biomass_enabled=True,
        ibm_granite_biomass_checkpoint_path=str(checkpoint),
        ibm_granite_biomass_config_path=str(config),
        ibm_granite_biomass_checkpoint_sha256="a" * 64,
    )
    monkeypatch.setattr(registry, "_runtime_has_granite", lambda _python: True)

    model = registry.granite_biomass_model(settings)
    assert model is not None
    assert model["metadata_json"]["provider"] == "ibm_granite_hls"
    assert model["metadata_json"]["gee_deployable"] is False
    assert model["metadata_json"]["compatible_reference_datasets"] == ["GEDI_L4A_MONTHLY"]


def test_granite_model_listing_obeys_dataset_and_gee_filters(monkeypatch):
    granite = {
        "name": registry.MODEL_NAME,
        "metadata_json": {
            "provider": "ibm_granite_hls",
            "gee_deployable": False,
            "compatible_reference_datasets": [registry.TARGET_DATASET],
        },
    }
    monkeypatch.setattr(model_routes, "granite_biomass_model", lambda: granite)
    monkeypatch.setattr(model_routes, "builtin_carbon_models", lambda *_args, **_kwargs: [])

    listed = model_routes.models_list(
        model_type="carbon", target_dataset=registry.TARGET_DATASET, gee_deployable=False, db=EmptyDB()
    )
    assert [item["name"] for item in listed["models"]] == [registry.MODEL_NAME]
    excluded = model_routes.models_list(
        model_type="carbon", target_dataset="GEDI_L4B_STACK", gee_deployable=False, db=EmptyDB()
    )
    assert excluded["count"] == 0
    gee_only = model_routes.models_list(
        model_type="carbon", target_dataset=registry.TARGET_DATASET, gee_deployable=True, db=EmptyDB()
    )
    assert gee_only["count"] == 0


def test_active_model_compatibility_includes_granite_only_when_runtime_is_ready(monkeypatch):
    model = {
        "name": registry.MODEL_NAME,
        "metadata_json": {"compatible_reference_datasets": [registry.TARGET_DATASET]},
    }
    monkeypatch.setattr(carbon_service, "builtin_carbon_models", lambda **_kwargs: [])
    monkeypatch.setattr(carbon_service, "granite_biomass_model", lambda: model)
    assert carbon_service.active_carbon_model_compatibility(EmptyDB()) == {
        registry.TARGET_DATASET: [registry.MODEL_NAME]
    }


def test_granite_uses_its_dedicated_carbon_local_pipeline(monkeypatch):
    expected = {"inference_mode": "ibm_granite_hls_stats"}
    monkeypatch.setattr(granite_biomass_service, "analyze_granite_biomass", lambda _db, _data: expected)
    result = carbon_service.analyze_carbon_local(
        EmptyDB(), {"model_name": registry.MODEL_NAME, "aoi": {}}
    )
    assert result is expected


def test_granite_period_and_prediction_output_validation():
    year, first_month, last_month, start, end = granite_biomass_service._analysis_period(
        {"year": 2025, "start_month": 2, "end_month": 4}
    )
    assert (year, first_month, last_month, start, end) == (2025, 2, 4, "2025-02-01", "2025-05-01")
    with pytest.raises(AnalysisError) as exc_info:
        granite_biomass_service._analysis_period({"year": 2025, "start_month": 5, "end_month": 4})
    assert getattr(exc_info.value, "status_code", None) == 400

    import numpy as np

    result = granite_biomass_service._prediction_array(np.ones((1, 1, 4, 5), dtype=np.float32))
    assert result.shape == (4, 5)


def test_granite_inference_uses_isolated_runtime_and_validates_worker_response(tmp_path, monkeypatch):
    from types import SimpleNamespace

    input_dir = tmp_path / "input"
    checkpoint = tmp_path / "model.ckpt"
    config = tmp_path / "config.yaml"
    output = tmp_path / "prediction.npy"
    monkeypatch.setenv("GRANITE_PYTHON", "/opt/granite/bin/python")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[6]).write_text('{"filenames":["hls_l30_aoi.tif"]}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(granite_biomass_service.subprocess, "run", fake_run)
    assert granite_biomass_service._run_model_inference(input_dir, checkpoint, config, output) == [
        "hls_l30_aoi.tif"
    ]
    command, options = calls[0]
    assert command[0] == "/opt/granite/bin/python"
    assert Path(command[1]).name == "granite_worker.py"
    assert command[2:7] == [
        str(input_dir),
        str(checkpoint),
        str(config),
        str(output),
        str(output.with_suffix(".json")),
    ]
    assert options["timeout"] == 900
    assert options["check"] is True


def test_granite_input_raster_preserves_ibm_band_order_and_valid_mask(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    source_path = tmp_path / "hls.tif"
    prediction_dir = tmp_path / "predict"
    prediction_dir.mkdir()
    profile = {
        "driver": "GTiff",
        "width": 3,
        "height": 2,
        "count": 7,
        "dtype": "int16",
        "crs": "EPSG:32648",
        "transform": from_origin(500000, 1000000, 30, 30),
    }
    with rasterio.open(source_path, "w", **profile) as raster:
        for band in range(1, 7):
            raster.write(np.full((2, 3), band * 100, dtype=np.int16), band)
        raster.write(np.array([[1, 0, 1], [0, 1, 1]], dtype=np.int16), 7)

    model_path, valid = granite_biomass_service._prepare_model_raster(source_path, prediction_dir)
    with rasterio.open(model_path) as raster:
        assert raster.count == 11
        assert np.all(raster.read(1) == 0)
        assert [int(raster.read(band)[0, 0]) for band in range(2, 8)] == [100, 200, 300, 400, 500, 600]
        assert all(np.all(raster.read(band) == 0) for band in range(8, 12))
    assert valid.tolist() == [[True, False, True], [False, True, True]]
