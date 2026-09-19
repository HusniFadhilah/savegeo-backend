"""Try contextual calibration models without hiding validation leakage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, SplineTransformer, StandardScaler


def metric(y: np.ndarray, p: np.ndarray) -> dict[str, float | int]:
    residual = p - y
    total = float(np.sum((y - y.mean()) ** 2))
    return {
        "n": int(len(y)),
        "bias": float(residual.mean()),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mae": float(np.mean(np.abs(residual))),
        "r2": None if total == 0 else float(1.0 - np.sum(residual**2) / total),
    }


def load_rows(path: Path, area_m2: float) -> list[dict]:
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for run in report["runs"]:
        for item in run["plots"]:
            if item.get("predicted_mg_c_ha") is None or item["field_status"] != "valid":
                continue
            rows.append(
                {
                    "plot_id": f"{run['dataset_id']}::{item['plot_id']}",
                    "dataset_id": run["dataset_id"],
                    "site_id": item["site_id"],
                    "x": float(item["predicted_mg_c_ha"]),
                    "y": float(item["observed_kg_c_plot"]) * 10.0 / area_m2,
                }
            )
    return rows


def contextual_model(method: str):
    encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    preprocess = ColumnTransformer(
        [
            ("numeric", StandardScaler(), ["x"]),
            ("categorical", encoder, ["site_id", "dataset_id"]),
        ],
        remainder="drop",
    )
    if method == "ridge_site":
        return make_pipeline(preprocess, Ridge(alpha=10.0))
    if method == "ridge_site_low_penalty":
        return make_pipeline(preprocess, Ridge(alpha=1.0))
    if method == "gradient_boosting_site":
        return make_pipeline(preprocess, GradientBoostingRegressor(
            n_estimators=80, learning_rate=0.03, max_depth=2, min_samples_leaf=4, random_state=42
        ))
    if method == "random_forest_site":
        return make_pipeline(preprocess, RandomForestRegressor(
            n_estimators=300, max_depth=3, min_samples_leaf=3, random_state=42, n_jobs=1
        ))
    raise ValueError(method)


def fit_predict(train: list[dict], test: list[dict], method: str) -> np.ndarray:
    train_df = pd.DataFrame(train)
    test_df = pd.DataFrame(test)
    if method == "site_residual":
        global_bias = float((train_df["y"] - train_df["x"]).mean())
        site_bias = (train_df["y"] - train_df["x"]).groupby(train_df["site_id"]).mean().to_dict()
        return np.asarray([row["x"] + site_bias.get(row["site_id"], global_bias) for row in test], dtype=float)
    if method == "site_mean":
        global_mean = float(train_df["y"].mean())
        site_mean = train_df.groupby("site_id")["y"].mean().to_dict()
        return np.asarray([site_mean.get(row["site_id"], global_mean) for row in test], dtype=float)
    if method == "spline_x":
        model = make_pipeline(
            SplineTransformer(n_knots=4, degree=2, include_bias=False),
            Ridge(alpha=10.0),
        )
        model.fit(train_df[["x"]], train_df["y"])
        return np.asarray(model.predict(test_df[["x"]]), dtype=float)
    model = contextual_model(method)
    model.fit(train_df[["x", "site_id", "dataset_id"]], train_df["y"])
    return np.asarray(model.predict(test_df[["x", "site_id", "dataset_id"]]), dtype=float)


def cross_validate(rows: list[dict], method: str, group_key: str) -> dict:
    groups = list(dict.fromkeys(row[group_key] for row in rows))
    y_true, y_pred = [], []
    for group in groups:
        train = [row for row in rows if row[group_key] != group]
        test = [row for row in rows if row[group_key] == group]
        if not train or not test:
            continue
        predictions = fit_predict(train, test, method)
        y_true.extend(row["y"] for row in test)
        y_pred.extend(np.maximum(0.0, predictions))
    return metric(np.asarray(y_true, dtype=float), np.asarray(y_pred, dtype=float))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="var/reports/carbon_calibration_probe_20260917.json")
    parser.add_argument("--area-m2", type=float, default=400.0)
    parser.add_argument("--output", default="var/reports/advanced_calibration_comparison_400m2.json")
    args = parser.parse_args()

    rows = load_rows(Path(args.report), args.area_m2)
    methods = ["site_residual", "site_mean", "spline_x", "ridge_site", "ridge_site_low_penalty", "gradient_boosting_site", "random_forest_site"]
    result = {
        "status": "computed",
        "area_m2": args.area_m2,
        "warning": "400 m2 is provisional; contextual models may not generalize to unseen sites",
        "validation": {},
    }
    for validation_name, group_key in [("leave_one_plot_out", "plot_id"), ("leave_one_site_out", "site_id")]:
        result["validation"][validation_name] = {
            method: cross_validate(rows, method, group_key) for method in methods
        }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
