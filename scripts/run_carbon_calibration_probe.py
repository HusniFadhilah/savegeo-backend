"""Run an auditable GEE baseline probe against the carbon survey workbooks.

This command deliberately does not assume a plot area. It produces model
predictions in Mg C/ha and keeps field totals in kg C/plot. Supplying
--plot-area-m2 enables an explicitly labelled scenario evaluation, but the
area is never inferred from workbook formulas.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from statistics import median

import ee

from app.core.config import get_settings
from app.inference.carbon_inference import CarbonInferenceEngine
from app.services.carbon_calibration_service import evaluate_predictions, lopo_linear_calibration
from app.services.carbon_workbook_service import parse_workbook
from app.services.gee_service import _init_from_key_file


LOG = logging.getLogger("carbon_calibration_probe")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--plot-area-m2", type=float, default=None)
    parser.add_argument("--cloud-threshold", type=int, default=70)
    parser.add_argument("--scale", type=int, default=250)
    return parser.parse_args()


def _init_ee() -> None:
    settings = get_settings()
    if not settings.gee_service_account or not settings.gee_key_file:
        raise RuntimeError("GEE_SERVICE_ACCOUNT and GEE_KEY_FILE are required")
    _init_from_key_file(settings.gee_service_account, settings.gee_key_file)


def _plot_rows(parsed: dict) -> list[dict]:
    records_by_plot: dict[str, list[dict]] = defaultdict(list)
    for record in parsed["records"]:
        records_by_plot[record["plot_id"]].append(record)

    result = []
    for summary in parsed["audit"]["plot_reconciliation"]:
        records = records_by_plot[summary["plot_id"]]
        coords = [
            (float(record["latitude"]), float(record["longitude"]))
            for record in records
            if record["coordinate_status"] == "valid"
            and record["latitude"] is not None
            and record["longitude"] is not None
        ]
        result.append(
            {
                "plot_id": summary["plot_id"],
                "site_id": summary["site_id"],
                "field_status": summary["status"],
                "valid_tree_count": summary["valid_tree_count"],
                "observed_kg_c_plot": float(summary["agb_carbon_kg_plot"]),
                "latitude": median([item[0] for item in coords]) if coords else None,
                "longitude": median([item[1] for item in coords]) if coords else None,
            }
        )
    return result


def _predict_plots(engine: CarbonInferenceEngine, plots: list[dict], year: int, *, scale: int, cloud_threshold: int) -> dict:
    eligible = [item for item in plots if item["latitude"] is not None and item["longitude"] is not None]
    if not eligible:
        return {"status": "not_computed", "reason": "No valid plot coordinates", "plots": []}

    features = [
        ee.Feature(
            ee.Geometry.Point([item["longitude"], item["latitude"]]),
            {"plot_id": item["plot_id"], "site_id": item["site_id"]},
        )
        for item in eligible
    ]
    points = ee.FeatureCollection(features)
    roi = points.geometry().bounds()
    image = engine.predict_for_region(
        roi,
        year=year,
        start_month=1,
        end_month=12,
        cloud_threshold=cloud_threshold,
        scale=scale,
    )
    sampled = image.sampleRegions(
        collection=points,
        properties=["plot_id", "site_id"],
        scale=scale,
        geometries=False,
    ).getInfo()

    predictions = {}
    for feature in sampled.get("features", []):
        props = feature.get("properties", {})
        value = props.get("carbon_estimated")
        if value is not None:
            predictions[str(props["plot_id"])] = float(value)

    rows = []
    for item in plots:
        predicted = predictions.get(item["plot_id"])
        output = {**item, "predicted_mg_c_ha": predicted}
        if predicted is not None:
            rows.append(output)

    return {
        "status": "computed",
        "year": year,
        "scale_m": scale,
        "cloud_threshold": cloud_threshold,
        "s2_image_count": engine.last_s2_image_count,
        "valid_pixel_pct": engine.last_valid_pixel_pct,
        "gap_filled": engine.last_gap_filled,
        "plot_count_with_coordinates": len(eligible),
        "plot_count_predicted": len(predictions),
        "plots": rows,
    }


def _evaluate(prediction_result: dict, plot_area_m2: float | None) -> dict:
    if prediction_result.get("status") != "computed":
        return {"status": "not_computed", "reason": prediction_result.get("reason", "prediction failed")}

    if plot_area_m2 is None:
        return {
            "status": "not_computed",
            "reason": "Plot area is not confirmed; model output is Mg C/ha while field totals are kg C/plot",
            "unit_block": True,
        }
    if plot_area_m2 <= 0:
        raise ValueError("--plot-area-m2 must be positive")

    rows = []
    for item in prediction_result["plots"]:
        if item["predicted_mg_c_ha"] is None or item["field_status"] != "valid":
            continue
        observed = item["observed_kg_c_plot"] * 10.0 / plot_area_m2
        rows.append({
            "plot_id": item["plot_id"],
            "predicted": item["predicted_mg_c_ha"],
            "observed": observed,
        })
    return {
        "status": "computed",
        "area_scenario_m2": plot_area_m2,
        "scenario_warning": "Non-production scenario unless plot area is independently confirmed",
        "baseline": evaluate_predictions(rows),
        "lopo_linear": lopo_linear_calibration(rows),
    }


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _init_ee()
    settings = get_settings()
    model_path = settings.model_path / "gedi_l4a_monthly_s2_dem_hgb_2023.pkl"
    if not model_path.exists():
        raise FileNotFoundError(model_path)
    model_name = "gedi_l4a_monthly_s2_dem_hgb_2023"
    engine = CarbonInferenceEngine(model_name=model_name, model_path=str(model_path))

    sources = [
        (Path(r"L:\Husni\Penelitian\Hiliriset\Eksperimen\Dataset\Data Carbon Survey\Bu Dessy\Data Survei Carbon.xlsx"), 2024, "previous_2024"),
        (Path(r"L:\Husni\Penelitian\Hiliriset\Eksperimen\Dataset\Data Carbon Survey\Bu Dessy\OLAH DATA CARBON (REVISI).xlsx"), 2026, "revised_2026"),
    ]
    output = {
        "status": "computed",
        "model": {
            "name": engine.model_name,
            "path": str(model_path),
            "target_unit": engine.model.metadata.get("target_unit"),
            "feature_set": engine.model.metadata.get("feature_set_name"),
        },
        "plot_area_m2": args.plot_area_m2,
        "runs": [],
    }
    for path, year, dataset_id in sources:
        LOG.info("Parsing %s", path.name)
        parsed = parse_workbook(path, dataset_id, year)
        plots = _plot_rows(parsed)
        LOG.info("Predicting %s valid-coordinate plots for %s", sum(item["latitude"] is not None for item in plots), dataset_id)
        prediction = _predict_plots(engine, plots, year, scale=args.scale, cloud_threshold=args.cloud_threshold)
        prediction["dataset_id"] = dataset_id
        prediction["source"] = parsed["source"]
        prediction["audit"] = parsed["audit"]
        prediction["evaluation"] = _evaluate(prediction, args.plot_area_m2)
        output["runs"].append(prediction)

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "status": output["status"],
        "output": str(destination),
        "model": output["model"]["name"],
        "runs": [
            {
                "dataset_id": run["dataset_id"],
                "prediction_status": run["status"],
                "predicted": run.get("plot_count_predicted", 0),
                "evaluation": run["evaluation"].get("status"),
            }
            for run in output["runs"]
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
