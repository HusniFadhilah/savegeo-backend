"""Evaluate the two saved GEDI L4D models on the existing survey plot probe."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import ee
import numpy as np
import pandas as pd

from app.core.config import get_settings
from app.inference.carbon_inference import CarbonInferenceEngine
from app.services.carbon_calibration_service import evaluate_predictions, lopo_linear_calibration
from app.services.gee_service import _init_from_key_file


LOG = logging.getLogger("unapplied_carbon_models")
MODEL_NAMES = ["gedi_l4d_ridge_s2_dem_lc_2023", "gedi_l4d_best_cv_model_2023"]


def sample_model(engine: CarbonInferenceEngine, plots: list[dict], year: int, scale: int, cloud_threshold: int) -> dict:
    eligible = [item for item in plots if item.get("latitude") is not None and item.get("longitude") is not None]
    if not eligible:
        return {"status": "not_computed", "reason": "No valid plot coordinates", "plots": []}

    points = ee.FeatureCollection([
        ee.Feature(
            ee.Geometry.Point([item["longitude"], item["latitude"]]),
            {"plot_id": item["plot_id"], "site_id": item["site_id"]},
        )
        for item in eligible
    ])
    roi = points.geometry().bounds()
    gee_type = engine.model.metadata.get("gee_algorithm_type", "")

    if gee_type in {"native_classifier", "linear_expression"}:
        image = engine.predict_for_region(
            roi,
            year=year,
            start_month=1,
            end_month=12,
            cloud_threshold=cloud_threshold,
            scale=scale,
        )
        feature_collection = image.sampleRegions(
            collection=points,
            properties=["plot_id", "site_id"],
            scale=scale,
            geometries=False,
        ).getInfo()
        values = {
            str(feature["properties"]["plot_id"]): feature["properties"].get("carbon_estimated")
            for feature in feature_collection.get("features", [])
        }
    else:
        LOG.info("Sampling local server-side feature stack for %s", engine.model_name)
        predictors = engine._build_predictors(
            roi=roi,
            year=year,
            start_month=1,
            end_month=12,
            cloud_threshold=cloud_threshold,
        )
        sampled = predictors.sampleRegions(
            collection=points,
            properties=["plot_id", "site_id"],
            scale=scale,
            geometries=False,
        ).getInfo()
        expected = engine._get_expected_features()
        properties = [feature.get("properties", {}) for feature in sampled.get("features", [])]
        frame = pd.DataFrame(properties)
        values = {}
        if not frame.empty:
            frame["plot_id"] = frame["plot_id"].astype(str)
            usable = frame.dropna(subset=expected)
            if not usable.empty:
                X = usable[expected].to_numpy(dtype=float)
                if hasattr(engine.model.scaler, "mean_") and engine.model.metadata.get("scaled_features", True):
                    X = engine.model.scaler.transform(X)
                predictions = np.clip(engine.model.model.predict(X), 0, None)
                values = dict(zip(usable["plot_id"], predictions.astype(float)))

    result_plots = [{**item, "predicted_mg_c_ha": float(values[item["plot_id"]]) if item["plot_id"] in values else None} for item in plots]
    return {
        "status": "computed",
        "year": year,
        "scale_m": scale,
        "cloud_threshold": cloud_threshold,
        "gee_algorithm_type": gee_type,
        "plot_count_with_coordinates": len(eligible),
        "plot_count_predicted": len(values),
        "s2_image_count": engine.last_s2_image_count,
        "plots": result_plots,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="var/reports/carbon_calibration_probe_20260917.json")
    parser.add_argument("--output", default="var/reports/unapplied_carbon_models_20260917.json")
    parser.add_argument("--scale", type=int, default=250)
    parser.add_argument("--cloud-threshold", type=int, default=70)
    args = parser.parse_args()

    settings = get_settings()
    _init_from_key_file(settings.gee_service_account, settings.gee_key_file)
    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = {
        "status": "computed",
        "source_probe": args.input,
        "models": {},
        "warnings": [
            "Observed values use the existing provisional 400 m2 scenario from the source probe.",
            "GEDI L4D Extra Trees is server-side only and is evaluated from sampled GEE feature stacks.",
        ],
    }

    for model_name in MODEL_NAMES:
        model_path = settings.model_path / f"{model_name}.pkl"
        engine = CarbonInferenceEngine(model_name=model_name, model_path=str(model_path))
        model_result = {
            "model_name": model_name,
            "algorithm": engine.model.algorithm,
            "target_unit": engine.model.metadata.get("target_unit"),
            "feature_set": engine.model.metadata.get("feature_stack") or engine.model.metadata.get("feature_set_name"),
            "gee_algorithm_type": engine.model.metadata.get("gee_algorithm_type"),
            "runs": [],
        }
        for source_run in source["runs"]:
            dataset_id = source_run["dataset_id"]
            year = 2024 if dataset_id == "previous_2024" else 2026
            LOG.info("%s: evaluating %s", model_name, dataset_id)
            try:
                result = sample_model(engine, source_run["plots"], year, args.scale, args.cloud_threshold)
                result["dataset_id"] = dataset_id
                valid_rows = [
                    {
                        "plot_id": item["plot_id"],
                        "predicted": item["predicted_mg_c_ha"],
                        "observed": item["observed_kg_c_plot"] * 10.0 / 400.0,
                    }
                    for item in result["plots"]
                    if item.get("predicted_mg_c_ha") is not None and item["field_status"] == "valid"
                ]
                result["baseline_400m2"] = evaluate_predictions(valid_rows)
                result["lopo_linear_400m2"] = lopo_linear_calibration(valid_rows)
            except Exception as exc:  # noqa: BLE001 - preserve per-model diagnostic result
                LOG.exception("%s failed for %s", model_name, dataset_id)
                result = {"status": "failed", "dataset_id": dataset_id, "error": str(exc), "plots": []}
            model_result["runs"].append(result)
        output["models"][model_name] = model_result

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "status": output["status"],
        "output": str(destination),
        "models": {
            name: [
                {
                    "dataset_id": run["dataset_id"],
                    "status": run["status"],
                    "predicted": run.get("plot_count_predicted", 0),
                    "baseline_r2": run.get("baseline_400m2", {}).get("r2"),
                    "lopo_r2": run.get("lopo_linear_400m2", {}).get("r2"),
                }
                for run in model["runs"]
            ]
            for name, model in output["models"].items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
