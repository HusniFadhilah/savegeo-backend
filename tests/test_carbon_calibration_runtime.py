import hashlib
import json

from app.db.models.carbon_calibration import CarbonCalibrationDataset, CarbonCalibrationModel, CarbonCalibrationRun, FieldPlot
from app.services.calibration_review import manifest
from app.services.carbon_calibration_runtime import apply_selected_calibration, resolve_calibration_for_raster

pytest_plugins = ["tests.test_organizations"]


def seeded_runtime_model(factory, tmp_path, *, tile_url=None):
    domain = {"type": "Polygon", "coordinates": [[[106, -6], [107, -6], [107, -5], [106, -5], [106, -6]]]}
    baseline = {"id": "base-carbon", "version": "2.1", "artifact_sha256": "b" * 64,
                "preprocessing_version": "s2-sr-v2"}
    parameters = {"baseline_model": baseline, "training_plot_ids": ["p1", "p2", "p3"],
                  "validation_plot_ids": ["p4", "p5", "p6"]}
    metrics = {"status": "computed", "sample_count": 6, "validation_type": "independent_field_plot_holdout",
               "independent_validation_metrics": {"status": "computed", "n_plots": 3, "rmse": 2.0},
               "validation_plot_ids": ["p4", "p5", "p6"]}
    artifact = {"target_pool": "aboveground_biomass_carbon", "target_unit": "Mg C/ha",
                "baseline_model": baseline, "compatible_sensors": ["sentinel-2"],
                "application_domain_geojson": domain, "compatible_analysis_years": [2025],
                "intercept": 2.0, "slope": 1.5, "clip_min": 0.0,
                "validation": {"validation_type": "independent_field_plot_holdout",
                               "independent_validation_metrics": metrics["independent_validation_metrics"]}}
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with factory() as db:
        dataset = CarbonCalibrationDataset(id=1, dataset_id="local-domain", name="Local field set",
            manifest={"site": "test", "application_domain_geojson": domain, "compatible_sensors": ["sentinel-2"]},
            validation_report={})
        db.add(dataset)
        db.flush()
        for plot_id in ("p1", "p2", "p3", "p4", "p5", "p6"):
            db.add(FieldPlot(dataset_pk=1, plot_id=plot_id, qa_status="valid", attributes={}))
        run = CarbonCalibrationRun(id="runtime-run", dataset_pk=1, status="complete", method="linear_calibration",
            target_pool="aboveground_biomass_carbon", parameters=parameters, metrics=metrics, created_by=1)
        db.add(run)
        db.flush()
        model = CarbonCalibrationModel(id=1, model_id="local-cal-v1", dataset_pk=1, run_id=run.id,
            model_name="Local field calibration", version="1", target_pool="aboveground_biomass_carbon",
            target_unit="Mg C/ha", status="active_local", artifact_path=str(path), artifact_sha256=digest,
            metadata_json={}, is_active=True, approved_by=2)
        db.add(model)
        db.flush()
        model.metadata_json = {"approval_manifest": manifest(model, run, dataset)}
        db.commit()


def result(*, tile_url=None):
    estimated = {"unit": "Mg C/ha", "target_pool": "aboveground_biomass_carbon",
                 "statistics": {"mean": 10.0, "min": 4.0, "max": 20.0, "std_dev": 2.0,
                                "percentiles": {"p25": 8.0}, "histogram": {"bucket_means": [5.0, 15.0], "counts": [2, 3]}}}
    if tile_url:
        estimated["tile_url"] = tile_url
    return {"carbon_estimated": estimated, "area_info": {"area_ha": 5.0},
            "model_info": {"target_pool": "aboveground_biomass_carbon"},
            "data_quality": {"source_provenance": [{"collection_id": "COPERNICUS/S2_SR_HARMONIZED"}]}}


def runtime_inputs():
    return ({"calibration_model_id": 1, "year": 2025,
             "aoi": {"type": "Polygon", "coordinates": [[[106.2, -5.8], [106.4, -5.8], [106.4, -5.6], [106.2, -5.6], [106.2, -5.8]]]}},
            {"id": 9, "name": "base-carbon", "version": "2.1", "artifact_sha256": "b" * 64,
             "preprocessing_version": "s2-sr-v2"})


def test_runtime_applies_reviewed_calibration_to_saved_statistics_only(env, tmp_path):
    _, factory, _ = env
    seeded_runtime_model(factory, tmp_path)
    payload, pin = runtime_inputs()
    with factory() as db:
        updated = apply_selected_calibration(db, result(), payload, model_pin=pin)
    assert updated["carbon_estimated"]["statistics"]["mean"] == 17.0
    assert updated["carbon_estimated"]["statistics"]["std_dev"] == 3.0
    assert updated["area_info"]["total_carbon_tons"] == 85.0
    assert updated["model_info"]["calibration"]["status"] == "applied"
    assert updated["model_info"]["calibration"]["application_mode"] == "statistics_only_no_map_layer"


def test_runtime_does_not_calibrate_summary_while_base_map_tile_is_returned(env, tmp_path):
    _, factory, _ = env
    seeded_runtime_model(factory, tmp_path)
    payload, pin = runtime_inputs()
    with factory() as db:
        updated = apply_selected_calibration(db, result(tile_url="https://tiles.example/map/{z}/{x}/{y}"), payload, model_pin=pin)
    assert updated["carbon_estimated"]["statistics"]["mean"] == 10.0
    assert updated["model_info"]["calibration"]["status"] == "not_applied"
    assert updated["model_info"]["calibration"]["reason"] == "pixel_level_transform_not_supported_by_analysis_output"


def test_raster_resolver_requires_a_verified_year_and_returns_pixel_transform(env, tmp_path):
    _, factory, _ = env
    seeded_runtime_model(factory, tmp_path)
    payload, pin = runtime_inputs()
    with factory() as db:
        artifact, info = resolve_calibration_for_raster(
            db,
            payload,
            model_pin=pin,
            target_pool="aboveground_biomass_carbon",
            unit="Mg C/ha",
            source_provenance=[{"collection_id": "COPERNICUS/S2_SR_HARMONIZED"}],
        )
        wrong_year, wrong_year_info = resolve_calibration_for_raster(
            db,
            {**payload, "year": 2024},
            model_pin=pin,
            target_pool="aboveground_biomass_carbon",
            unit="Mg C/ha",
            source_provenance=[{"collection_id": "COPERNICUS/S2_SR_HARMONIZED"}],
        )
    assert artifact is not None
    assert artifact["slope"] == 1.5
    assert artifact["intercept"] == 2.0
    assert info["application_mode"] == "pixel_level_raster"
    assert wrong_year is None
    assert wrong_year_info["reason"] == "calibration_analysis_year_unsupported"
