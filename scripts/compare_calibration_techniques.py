"""Compare plot-level calibration techniques with leave-one-plot-out validation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import HuberRegressor, LinearRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float | int]:
    residual = y_pred - y_true
    ss_total = float(np.sum((y_true - y_true.mean()) ** 2))
    return {
        "n": int(len(y_true)),
        "bias": float(residual.mean()),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mae": float(np.mean(np.abs(residual))),
        "r2": None if ss_total == 0 else float(1 - np.sum(residual**2) / ss_total),
    }


def rows_from_report(report: dict, area_m2: float) -> list[dict]:
    rows = []
    for run in report["runs"]:
        for item in run["plots"]:
            if item.get("predicted_mg_c_ha") is None or item["field_status"] != "valid":
                continue
            rows.append(
                {
                    "plot_id": f"{run['dataset_id']}::{item['plot_id']}",
                    "site_id": f"{run['dataset_id']}::{item['site_id']}",
                    "x": float(item["predicted_mg_c_ha"]),
                    "y": float(item["observed_kg_c_plot"]) * 10.0 / area_m2,
                }
            )
    return rows


def fit_predict(method: str, x_train: np.ndarray, y_train: np.ndarray, x_test: float) -> float:
    if method == "bias_only":
        return float(x_test + np.mean(y_train - x_train))
    if method == "linear":
        model = LinearRegression().fit(x_train.reshape(-1, 1), y_train)
        return float(model.predict([[x_test]])[0])
    if method == "ridge":
        model = Ridge(alpha=10.0).fit(x_train.reshape(-1, 1), y_train)
        return float(model.predict([[x_test]])[0])
    if method == "log_linear":
        model = LinearRegression().fit(np.log1p(x_train).reshape(-1, 1), np.log1p(y_train))
        return float(np.expm1(model.predict([[np.log1p(x_test)]])[0]))
    if method == "huber":
        model = HuberRegressor(epsilon=1.35, alpha=0.0, max_iter=500).fit(
            x_train.reshape(-1, 1), y_train
        )
        return float(model.predict([[x_test]])[0])
    if method == "polynomial_2":
        model = make_pipeline(PolynomialFeatures(degree=2, include_bias=False), LinearRegression())
        model.fit(x_train.reshape(-1, 1), y_train)
        return float(model.predict([[x_test]])[0])
    if method == "isotonic":
        model = IsotonicRegression(out_of_bounds="clip").fit(x_train, y_train)
        return float(model.predict([x_test])[0])
    if method == "random_forest":
        model = RandomForestRegressor(
            n_estimators=200,
            max_depth=3,
            min_samples_leaf=3,
            random_state=42,
        ).fit(x_train.reshape(-1, 1), y_train)
        return float(model.predict([[x_test]])[0])
    raise ValueError(method)


def evaluate(rows: list[dict], method: str) -> dict:
    x = np.asarray([row["x"] for row in rows], dtype=float)
    y = np.asarray([row["y"] for row in rows], dtype=float)
    predictions = []
    for index, row in enumerate(rows):
        train = np.ones(len(rows), dtype=bool)
        train[index] = False
        prediction = fit_predict(method, x[train], y[train], row["x"])
        predictions.append(max(0.0, prediction))
    return metrics(y, np.asarray(predictions, dtype=float))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="var/reports/carbon_calibration_probe_20260917.json")
    parser.add_argument("--area-m2", type=float, default=400.0)
    parser.add_argument("--output", default="var/reports/calibration_technique_comparison_400m2.json")
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = rows_from_report(report, args.area_m2)
    methods = ["identity", "bias_only", "linear", "ridge", "log_linear", "huber", "polynomial_2", "isotonic", "random_forest"]
    result = {
        "status": "computed",
        "area_m2": args.area_m2,
        "validation": "leave_one_plot_out",
        "warning": "400 m2 is a provisional scenario; predictions are 250 m point-support values",
        "datasets": {},
    }
    for dataset_id in ["previous_2024", "revised_2026", "combined"]:
        subset = rows if dataset_id == "combined" else [row for row in rows if row["plot_id"].startswith(dataset_id + "::")]
        result["datasets"][dataset_id] = {
            method: metrics(
                np.asarray([row["y"] for row in subset], dtype=float),
                np.asarray([row["x"] for row in subset], dtype=float),
            ) if method == "identity" else evaluate(subset, method)
            for method in methods
        }

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(destination), "datasets": result["datasets"]}, indent=2))


if __name__ == "__main__":
    main()
