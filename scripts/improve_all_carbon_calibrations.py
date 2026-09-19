"""Run the full, leakage-aware calibration comparison for every saved model."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from compare_advanced_calibration_techniques import cross_validate as contextual_cv  # noqa: E402
from compare_calibration_techniques import evaluate as scalar_cv  # noqa: E402
from compare_calibration_techniques import fit_predict as scalar_fit_predict  # noqa: E402


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


def build_rows(model: dict) -> list[dict]:
    rows = []
    for run in model["runs"]:
        for item in run.get("plots", []):
            if item.get("predicted_mg_c_ha") is None or item["field_status"] != "valid":
                continue
            rows.append(
                {
                    "plot_id": f"{run['dataset_id']}::{item['plot_id']}",
                    "dataset_id": run["dataset_id"],
                    "site_id": item["site_id"],
                    "x": float(item["predicted_mg_c_ha"]),
                    "y": float(item["observed_kg_c_plot"]) * 10.0 / 400.0,
                }
            )
    return rows


def scalar_rows(rows: list[dict]) -> list[dict]:
    return [
        {"plot_id": row["plot_id"], "x": row["x"], "y": row["y"]}
        for row in rows
    ]


def scalar_cv_with_methods(rows: list[dict]) -> dict:
    methods = ["bias_only", "linear", "ridge", "log_linear", "huber", "polynomial_2", "isotonic"]
    result = {}
    # The reusable scalar evaluator expects x/y fields and performs plot LOPO.
    for method in methods:
        result[method] = scalar_cv(scalar_rows(rows), method)
    return result


def quality_audit(rows: list[dict]) -> dict:
    y = np.asarray([row["y"] for row in rows], dtype=float)
    q1, q3 = np.quantile(y, [0.25, 0.75]) if len(y) else (None, None)
    iqr = (q3 - q1) if q1 is not None else None
    upper = q3 + 3.0 * iqr if iqr is not None else None
    flags = [row["plot_id"] for row in rows if upper is not None and row["y"] > upper]
    return {
        "n": len(rows),
        "observed_min": float(y.min()) if len(y) else None,
        "observed_median": float(np.median(y)) if len(y) else None,
        "observed_max": float(y.max()) if len(y) else None,
        "iqr_upper_review_threshold": float(upper) if upper is not None else None,
        "extreme_label_review_candidates": flags,
        "action": "review_only; no rows were removed from reported metrics",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="var/reports/unapplied_carbon_models_20260917.json")
    parser.add_argument("--output", default="var/reports/all_model_calibration_improvement_400m2.json")
    args = parser.parse_args()

    source = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = {
        "status": "computed",
        "area_m2": 400.0,
        "validation": ["leave_one_plot_out", "leave_one_site_out"],
        "warnings": [
            "400 m2 remains provisional and was not inferred as confirmed geometry.",
            "The supplied DEM/plot geometry is not used to fabricate footprint predictions.",
            "Extreme labels are reported for review only; no observation is silently removed.",
        ],
        "models": {},
    }

    for model_name, model in source["models"].items():
        rows = build_rows(model)
        contextual_methods = ["site_residual", "site_mean", "spline_x", "ridge_site", "ridge_site_low_penalty", "gradient_boosting_site"]
        output["models"][model_name] = {
            "n_valid_plots": len(rows),
            "quality_audit": quality_audit(rows),
            "scalar_lopo": scalar_cv_with_methods(rows),
            "contextual_lopo": {method: contextual_cv(rows, method, "plot_id") for method in contextual_methods},
            "contextual_loso": {method: contextual_cv(rows, method, "site_id") for method in contextual_methods},
        }

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "status": output["status"],
        "output": str(destination),
        "models": {
            name: {
                "n": item["n_valid_plots"],
                "quality_flags": len(item["quality_audit"]["extreme_label_review_candidates"]),
                "best_scalar_lopo": max(
                    ((method, values["r2"]) for method, values in item["scalar_lopo"].items() if values["r2"] is not None),
                    key=lambda pair: pair[1],
                ),
                "best_contextual_lopo": max(
                    ((method, values["r2"]) for method, values in item["contextual_lopo"].items() if values["r2"] is not None),
                    key=lambda pair: pair[1],
                ),
                "best_contextual_loso": max(
                    ((method, values["r2"]) for method, values in item["contextual_loso"].items() if values["r2"] is not None),
                    key=lambda pair: pair[1],
                ),
            }
            for name, item in output["models"].items()
        },
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
