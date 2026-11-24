"""Evaluate a SaveGeo carbon model with Dahana calibration and Bu Dessy holdout.

Predictions use the stored SaveGeo GEDI/Sentinel-2 model through the existing
CarbonInferenceEngine and Earth Engine. No catalog or database state is changed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyproj import Transformer

CARBON_FRACTION = 0.47
MODEL_NAME = "gedi_l4d_ridge_s2_dem_lc_2023"
MODEL_VERSION = "gedi-l4d-2023"
MODEL_SCALE_M = 30
MODEL_CLOUD_THRESHOLD = 70


def _finite_number(value: str, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _plot_summary(rows: list[dict[str, str]], *, dataset: str, plot_key: str,
                  year_key: str, area_key: str, biomass_key: str,
                  latitude_key: str, longitude_key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        plot_id = str(row.get(plot_key, "")).strip()
        if not plot_id:
            raise ValueError(f"{dataset}: plot identifier is missing")
        grouped[plot_id].append(row)

    plots = []
    for plot_id, tree_rows in sorted(grouped.items(), key=lambda item: item[0]):
        areas = {_finite_number(row.get(area_key), area_key) for row in tree_rows}
        years = {int(_finite_number(row.get(year_key), year_key)) for row in tree_rows}
        latitudes = {_finite_number(row.get(latitude_key), latitude_key) for row in tree_rows}
        longitudes = {_finite_number(row.get(longitude_key), longitude_key) for row in tree_rows}
        if len(areas) != 1 or len(years) != 1 or len(latitudes) != 1 or len(longitudes) != 1:
            raise ValueError(f"{dataset} plot {plot_id}: area, year, and coordinates must be consistent")
        area_m2 = areas.pop()
        latitude = latitudes.pop()
        longitude = longitudes.pop()
        year = years.pop()
        if area_m2 <= 0 or area_m2 > 4_000_000 or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError(f"{dataset} plot {plot_id}: area or coordinates are out of range")
        dry_biomass_kg = sum(_finite_number(row.get(biomass_key), biomass_key) for row in tree_rows)
        if dry_biomass_kg < 0:
            raise ValueError(f"{dataset} plot {plot_id}: biomass cannot be negative")
        observed_mg_c_ha = dry_biomass_kg * CARBON_FRACTION * 10 / area_m2
        plots.append({
            "dataset": dataset,
            "plot_id": plot_id,
            "survey_year": year,
            "plot_area_m2": area_m2,
            "latitude": latitude,
            "longitude": longitude,
            "tree_count": len(tree_rows),
            "dry_aboveground_biomass_kg": dry_biomass_kg,
            "observed_mg_c_ha": observed_mg_c_ha,
        })
    return plots


def load_dahana(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return _plot_summary(
            list(csv.DictReader(stream)), dataset="PT Dahana calibration",
            plot_key="plot_code", year_key="survey_year", area_key="plot_area_m2",
            biomass_key="biomass_kg", latitude_key="latitude", longitude_key="longitude",
        )


def load_bu_dessy(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return _plot_summary(
            list(csv.DictReader(stream)), dataset="Bu Dessy independent validation",
            plot_key="Point ID", year_key="Tahun", area_key="Luas Plot (m2)",
            biomass_key="Biomassa (kg)", latitude_key="Latitude", longitude_key="Longitude",
        )


def square_plot_bounds(longitude: float, latitude: float, area_m2: float) -> dict[str, float]:
    """Build the confirmed square-area proxy in local UTM coordinates."""
    zone = min(60, max(1, int((longitude + 180) // 6) + 1))
    epsg = (32700 if latitude < 0 else 32600) + zone
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    reverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    x, y = forward.transform(longitude, latitude)
    half = math.sqrt(area_m2) / 2
    corners = [reverse.transform(x - half, y - half), reverse.transform(x + half, y + half)]
    west, south = corners[0]
    east, north = corners[1]
    return {"west": west, "south": south, "east": east, "north": north}


def fit_bias_offset(calibration: list[dict[str, Any]]) -> float:
    pairs = [row for row in calibration if row.get("prediction_mg_c_ha") is not None]
    if len(pairs) < 3:
        raise ValueError("At least three valid Dahana plot predictions are required for calibration")
    residuals = [float(row["observed_mg_c_ha"]) - float(row["prediction_mg_c_ha"]) for row in pairs]
    return sum(residuals) / len(residuals)


def error_metrics(observed: list[float], predicted: list[float]) -> dict[str, Any]:
    if not observed or len(observed) != len(predicted):
        raise ValueError("Observed and predicted values must be non-empty and aligned")
    errors = [p - o for o, p in zip(observed, predicted, strict=True)]
    return {
        "n_plots": len(errors),
        "bias_mg_c_ha": sum(errors) / len(errors),
        "rmse_mg_c_ha": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "mae_mg_c_ha": sum(abs(error) for error in errors) / len(errors),
        "r2": None if len(errors) < 2 else _r2(observed, predicted),
        "qualification": "single_plot_preliminary_not_generalizable" if len(errors) == 1 else "preliminary",
    }


def failure_qualification(exc: Exception) -> str:
    """Classify an incomplete run without implying the model was unavailable."""
    if type(exc).__name__ in {"TransportError", "Timeout", "ConnectionError"}:
        return "not_computed_provider_transport_failure"
    if isinstance(exc, ValueError):
        return "not_computed_model_or_inference_failure"
    return "not_computed_evaluation_failure"


def _r2(observed: list[float], predicted: list[float]) -> float | None:
    mean = sum(observed) / len(observed)
    denominator = sum((value - mean) ** 2 for value in observed)
    if denominator <= 0:
        return None
    return 1 - sum((actual - guess) ** 2 for actual, guess in zip(observed, predicted, strict=True)) / denominator


def _square_plot_coordinates(longitude: float, latitude: float, area_m2: float) -> list[list[float]]:
    """Return a true metric square transformed from local UTM to lon/lat."""
    zone = min(60, max(1, int((longitude + 180) // 6) + 1))
    epsg = (32700 if latitude < 0 else 32600) + zone
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    reverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    x, y = forward.transform(longitude, latitude)
    half = math.sqrt(area_m2) / 2
    return [
        list(reverse.transform(x - half, y - half)),
        list(reverse.transform(x + half, y - half)),
        list(reverse.transform(x + half, y + half)),
        list(reverse.transform(x - half, y + half)),
        list(reverse.transform(x - half, y - half)),
    ]


def _predict_plot_means(
    engine, plots: list[dict[str, Any]], year: int,
) -> tuple[dict[str, float], int | None, dict[str, int]]:
    """Reduce SaveGeo's predicted raster over each exact plot footprint.

    Keep the inference request to one plot at a time. A single bounding box for
    a set of field plots can span a large area with no samples and exceed Earth
    Engine's native processing budget before zonal reduction begins.
    """
    import ee

    predictions, pixel_counts, image_counts = {}, {}, []
    reducer = ee.Reducer.mean().combine(
        ee.Reducer.count(), sharedInputs=True,
    )
    for plot in plots:
        coordinates = _square_plot_coordinates(
            float(plot["longitude"]), float(plot["latitude"]), float(plot["plot_area_m2"]),
        )
        geometry = ee.Geometry.Polygon([coordinates], proj="EPSG:4326", geodesic=False)
        plot_id = str(plot["plot_id"])
        collection = ee.FeatureCollection([ee.Feature(geometry, {"plot_id": plot_id})])
        roi = geometry.bounds(maxError=100)
        prediction_image = engine.predict_for_region(
            roi=roi,
            year=year,
            start_month=1,
            end_month=12,
            cloud_threshold=MODEL_CLOUD_THRESHOLD,
            scale=MODEL_SCALE_M,
        )
        result = prediction_image.reduceRegions(
            collection=collection,
            reducer=reducer,
            scale=MODEL_SCALE_M,
            tileScale=4,
        ).getInfo()
        image_count = engine.last_s2_image_count
        if isinstance(image_count, int):
            image_counts.append(image_count)
        for feature in result.get("features", []):
            properties = feature.get("properties") or {}
            returned_plot_id = str(properties.get("plot_id", ""))
            if returned_plot_id != plot_id:
                continue
            prediction = properties.get("carbon_estimated_mean", properties.get("carbon_estimated", properties.get("mean")))
            if prediction is not None:
                number = float(prediction)
                if math.isfinite(number):
                    predictions[plot_id] = max(0.0, number)
            count = properties.get("carbon_estimated_count", properties.get("count"))
            if count is not None:
                pixel_counts[plot_id] = int(count)
    return predictions, max(image_counts) if image_counts else None, pixel_counts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(args: argparse.Namespace) -> dict[str, Any]:
    calibration = load_dahana(args.dahana_csv)
    validation = load_bu_dessy(args.bu_dessy_csv)
    if len(validation) != 1:
        raise ValueError("This initial C13 report expects exactly one independent Bu Dessy plot")
    from app.core.config import get_settings
    from app.inference.carbon_inference import CarbonInferenceEngine
    from app.inference.carbon_model import CarbonEstimationModel
    from app.services.gee_service import _init_from_key_file

    settings = get_settings()
    model_path = Path(settings.model_path).expanduser().resolve() / f"{MODEL_NAME}.pkl"
    model = CarbonEstimationModel.load(str(model_path))
    metadata = model.metadata
    if metadata.get("gee_algorithm_type") != "linear_expression":
        raise ValueError("The chosen model must use the bounded linear-expression GEE inference path")
    engine = CarbonInferenceEngine(model_name=MODEL_NAME, model_path=str(model_path), quality_checks=False)
    _init_from_key_file(settings.gee_service_account, settings.gee_key_file, settings.gee_project_id)
    for year in sorted({int(plot["survey_year"]) for plot in calibration}):
        if year > datetime.now(UTC).year:
            raise ValueError("A field survey year cannot be later than the current year")
        year_plots = [plot for plot in calibration if int(plot["survey_year"]) == year]
        predictions, image_count, pixel_counts = _predict_plot_means(engine, year_plots, year)
        for plot in year_plots:
            prediction = predictions.get(str(plot["plot_id"]))
            plot.update(
                prediction_mg_c_ha=prediction,
                prediction_status="computed" if prediction is not None else "no_valid_pixels",
                prediction_year=year,
                source_images_used=image_count,
                model_scale_m=MODEL_SCALE_M,
                sampled_model_pixels=pixel_counts.get(str(plot["plot_id"])),
            )

    offset = fit_bias_offset(calibration)
    validation_plot = validation[0]
    year = int(validation_plot["survey_year"])
    if year > datetime.now(UTC).year:
        raise ValueError("A field survey year cannot be later than the current year")
    predictions, image_count, pixel_counts = _predict_plot_means(engine, validation, year)
    raw_prediction = predictions.get(str(validation_plot["plot_id"]))
    if raw_prediction is None:
        raise ValueError("Bu Dessy plot has no valid model pixels; C13 metrics cannot be computed")
    calibrated_prediction = max(0.0, raw_prediction + offset)
    validation_plot.update({
        "prediction_mg_c_ha": raw_prediction,
        "calibrated_prediction_mg_c_ha": calibrated_prediction,
        "prediction_status": "computed",
        "prediction_year": year,
        "source_images_used": image_count,
        "model_scale_m": MODEL_SCALE_M,
        "sampled_model_pixels": pixel_counts.get(str(validation_plot["plot_id"])),
    })
    observed = [float(validation_plot["observed_mg_c_ha"])]
    raw = [raw_prediction]
    calibrated = [calibrated_prediction]
    report = {
        "report_type": "savegeo_c13_preliminary_field_validation",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "computed",
        "model": {
            "name": MODEL_NAME, "version": MODEL_VERSION,
            "status": "offline_candidate_evaluation_no_activation",
            "provider": "SaveGeo CarbonInferenceEngine linear-expression raster inference",
            "target_pool": metadata.get("target_pool"),
            "training_year": metadata.get("training_year"),
            "training_region": metadata.get("training_region"),
            "training_cv_metrics": metadata.get("cv_metrics"),
            "carbon_fraction": CARBON_FRACTION,
            "native_scale_m": MODEL_SCALE_M,
            "model_artifact_sha256": metadata.get("artifact_sha256"),
            "model_file_sha256": _sha256(model_path),
            "field_support_note": "A metric square polygon is created from the measured plot area in local UTM coordinates, transformed to WGS84, then the predicted image is reduced with a mean over that exact footprint at the model's 30 m scale. The 20 x 20 m plot is smaller than a 30 m pixel, so this remains a coarse-support preliminary comparison and not a 20 m map validation.",
            "time_note": "The field files contain survey years but not dates. The 2024 Bu Dessy field year is later than this model's 2023 training year, and the annual Sentinel-2 composite may include imagery acquired after the field visit.",
            "catalog_or_database_changed": False,
        },
        "calibration": {
            "dataset": "PT Dahana 2026 field inventory",
            "dataset_sha256": _sha256(args.dahana_csv),
            "n_plots_total": len(calibration),
            "n_plots_with_prediction": sum(row.get("prediction_mg_c_ha") is not None for row in calibration),
            "method": "additive mean residual offset; fitted on Dahana plots only",
            "offset_mg_c_ha": offset,
            "field_basis": "dry above-ground biomass; measured plot area from the approved CSV; standard carbon fraction 0.47",
            "plot_results": calibration,
        },
        "validation": {
            "dataset": "Bu Dessy field inventory, independent of Dahana calibration",
            "dataset_sha256": _sha256(args.bu_dessy_csv),
            "n_plots": 1,
            "field_basis": "dry above-ground biomass; 20 x 20 m (400 m2); standard carbon fraction 0.47",
            "observed_mg_c_ha": observed[0],
            "plot_result": validation_plot,
            "raw_model_error": error_metrics(observed, raw),
            "after_dahana_calibration_error": error_metrics(observed, calibrated),
            "spatial_independence_note": "The field CSV is independent of the Dahana calibration data. However, this model was trained on GEDI reference data within a 1 degree training region that includes the Bu Dessy coordinate; this is not a spatially independent holdout relative to model training.",
            "interpretation": "One field plot provides an initial error check only. Do not present these values as general model accuracy or as a basis for broad deployment approval.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(args.output)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dahana-csv", type=Path, default=Path("examples/dmrv/pt_dahana_subang_field_inventory.csv"))
    parser.add_argument("--bu-dessy-csv", type=Path, default=Path("tests/fixtures/dmrv/bu_dessy_sample.csv"))
    parser.add_argument("--output", type=Path, default=Path("var/reports/c13_field_validation.json"))
    args = parser.parse_args()
    try:
        report = run(args)
    except Exception as exc:
        # Preserve a machine-readable evidence record when prerequisites are
        # missing. A blocked run must never look like a zero-error result.
        qualification = failure_qualification(exc)
        validation = load_bu_dessy(args.bu_dessy_csv)
        calibration = load_dahana(args.dahana_csv)
        report = {
            "report_type": "savegeo_c13_preliminary_field_validation",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "status": "not_computed",
            "reason": str(exc) if isinstance(exc, ValueError) else "Earth Engine or model evaluation failed before valid plot predictions were returned",
            "error_class": type(exc).__name__,
            "model": {
                "name": MODEL_NAME,
                "version": MODEL_VERSION,
                "status": "offline_candidate_evaluation_no_activation",
            },
            "calibration": {
                "dataset": "PT Dahana 2026 field inventory",
                "n_plots": len(calibration),
                "n_plots_with_prediction": 0,
            },
            "validation": {
                "dataset": "Bu Dessy independent plot",
                "n_plots": len(validation),
                "observed_mg_c_ha": validation[0]["observed_mg_c_ha"] if len(validation) == 1 else None,
                "prediction_mg_c_ha": None,
                "bias_mg_c_ha": None,
                "rmse_mg_c_ha": None,
                "mae_mg_c_ha": None,
                "qualification": qualification,
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(args.output)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 2
    print(json.dumps({
        "status": report["status"],
        "calibration_plots": report["calibration"]["n_plots_with_prediction"],
        "bu_dessy_observed_mg_c_ha": report["validation"]["observed_mg_c_ha"],
        "bu_dessy_prediction_mg_c_ha": report["validation"]["plot_result"]["prediction_mg_c_ha"],
        "raw_error": report["validation"]["raw_model_error"],
        "calibrated_error": report["validation"]["after_dahana_calibration_error"],
        "output": str(args.output),
    }, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
