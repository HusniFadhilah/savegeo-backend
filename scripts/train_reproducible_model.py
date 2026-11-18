"""Deterministic grouped-CV tabular regression to an inactive ONNX candidate.

The input table and parameters are tracked by the DVC stage. Candidate artifacts
are never activated by this script; promotion remains an administrator review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _versions() -> dict[str, str | None]:
    result = {}
    for package in ("scikit-learn", "numpy", "pandas", "skl2onnx", "onnx", "dvc", "mlflow"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    result["python"] = platform.python_version()
    result["platform"] = platform.platform()
    return result


def _resolved_distributions() -> dict[str, str]:
    """Record installed distribution versions without local paths or credentials."""
    resolved: dict[str, str] = {}
    for distribution in distributions():
        name = distribution.metadata.get("Name")
        if name:
            resolved[name.lower().replace("_", "-")] = distribution.version
    return dict(sorted(resolved.items()))


def _git_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
            timeout=5, cwd=Path(__file__).resolve().parents[1],
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_dirty() -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], check=True, capture_output=True, text=True,
            timeout=5, cwd=Path(__file__).resolve().parents[1],
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def _safe_tracking_endpoint(value: str) -> str:
    """Keep a useful MLflow destination label without persisting credentials or query tokens."""
    parsed = urlsplit(value)
    if not parsed.scheme:
        return "local_tracking_store"
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, "", "", ""))


def _safe_source_label(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if len(value) > 500:
        raise ValueError("dataset source/license labels must be at most 500 characters")
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname:
        hostname = parsed.hostname
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, hostname, "", "", ""))
    return value


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float | None]:
    residual = predicted - actual
    denominator = float(np.sum((actual - np.mean(actual)) ** 2))
    return {
        "r2": float(r2_score(actual, predicted)) if denominator else None,
        "rmse": float(np.sqrt(mean_squared_error(actual, predicted))),
        "mae": float(mean_absolute_error(actual, predicted)),
        "bias_prediction_minus_observed": float(np.mean(residual)),
    }


def _make_estimator(algorithm: str, seed: int, trees: int, max_depth: int | None):
    common = {"n_estimators": trees, "max_depth": max_depth, "random_state": seed, "n_jobs": 1}
    if algorithm == "random_forest":
        return RandomForestRegressor(**common, min_samples_leaf=2)
    if algorithm == "extra_trees":
        return ExtraTreesRegressor(**common, min_samples_leaf=2)
    return GradientBoostingRegressor(
        n_estimators=trees, max_depth=max_depth or 3, random_state=seed,
        min_samples_leaf=2, learning_rate=0.05,
    )


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(raw, encoding="utf-8")
    temporary.replace(path)


def train(args: argparse.Namespace) -> dict:
    source = Path(args.dataset).resolve()
    raw = source.read_bytes()
    frame = pd.read_csv(source)
    features = [item.strip() for item in args.features.split(",") if item.strip()]
    if not features or len(features) > 256 or len(features) != len(set(features)):
        raise ValueError("features must contain 1-256 distinct comma-separated column names")
    required = set(features) | {args.target, args.group}
    if len(required) != len(features) + 2 or set(frame.columns) != required:
        raise ValueError(f"CSV must contain exactly these columns: {', '.join(sorted(required))}")
    if frame.empty or len(frame) > 1_000_000:
        raise ValueError("Dataset must contain between 1 and 1,000,000 rows")
    groups = frame[args.group].astype(str).str.strip()
    values = frame[features + [args.target]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if groups.eq("").any() or not np.isfinite(values).all():
        raise ValueError("Dataset has empty group IDs or non-finite feature/target values")
    y = values[:, -1]
    if np.ptp(y) == 0:
        raise ValueError("Target values are constant")
    unique_groups = sorted(groups.unique().tolist())
    if len(unique_groups) < 5:
        raise ValueError("At least five independent groups are required for spatial/grouped validation")

    x = values[:, :-1]
    split_count = min(5, len(unique_groups))
    splitter = GroupKFold(n_splits=split_count)
    oof = np.full(len(y), np.nan, dtype=float)
    baseline_oof = np.full(len(y), np.nan, dtype=float)
    fold_metrics = []
    for fold, (train_index, validation_index) in enumerate(splitter.split(x, y, groups), start=1):
        estimator = _make_estimator(args.algorithm, args.seed, args.trees, args.max_depth)
        estimator.fit(x[train_index], y[train_index])
        oof[validation_index] = estimator.predict(x[validation_index])
        baseline_oof[validation_index] = float(np.mean(y[train_index]))
        fold_metrics.append({
            "fold": fold,
            "train_group_count": len(set(groups.iloc[train_index])),
            "validation_group_count": len(set(groups.iloc[validation_index])),
            **_metrics(y[validation_index], oof[validation_index]),
        })
    if not np.isfinite(oof).all():
        raise ValueError("Cross-validation did not produce predictions for every row")

    cv = _metrics(y, oof)
    baseline_cv = _metrics(y, baseline_oof)
    baseline_rmse_ratio = (cv["rmse"] / baseline_cv["rmse"]) if baseline_cv["rmse"] > 0 else float("inf")
    baseline_improved = baseline_rmse_ratio < args.max_baseline_rmse_ratio
    preflight_passed = cv["r2"] is not None and cv["r2"] >= args.minimum_r2 and baseline_improved
    duplicate_rows = int(frame.duplicated(subset=features + [args.target, args.group]).sum())
    group_sizes = groups.value_counts()
    preflight = {
        "status": "passed" if preflight_passed else "warning",
        "baseline_improved": baseline_improved,
        "baseline_rmse_ratio": baseline_rmse_ratio if np.isfinite(baseline_rmse_ratio) else None,
        "checks": {
            "finite_numeric_values": True,
            "non_empty_independent_groups": len(unique_groups) >= 5,
            "non_constant_target": bool(np.ptp(y) > 0),
            "grouped_out_of_fold_predictions": bool(np.isfinite(oof).all()),
            "minimum_r2_met": cv["r2"] is not None and cv["r2"] >= args.minimum_r2,
            "cross_validation_beats_mean_baseline_within_configured_ratio": baseline_improved,
        },
        "warnings": (["Ada baris pengukuran identik dalam dataset."] if duplicate_rows else []),
        "duplicate_sample_count": duplicate_rows,
        "group_size_summary": {
            "minimum": int(group_sizes.min()),
            "median": float(group_sizes.median()),
            "maximum": int(group_sizes.max()),
        },
        "message": None if preflight_passed else "Model harus memiliki R² positif dan mengungguli baseline mean pada grouped CV.",
    }
    if args.register and not preflight_passed:
        raise ValueError(preflight["message"] or "Quality preflight failed")
    estimator = _make_estimator(args.algorithm, args.seed, args.trees, args.max_depth)
    estimator.fit(x, y)
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType

    onnx_model = convert_sklearn(
        estimator,
        initial_types=[("features", FloatTensorType([None, len(features)]))],
        target_opset=15,
    )
    artifact = onnx_model.SerializeToString()
    artifact_hash = sha256(artifact)
    dataset_hash = sha256(raw)
    group_hash = sha256("\n".join(unique_groups).encode("utf-8"))
    dataset_source = _safe_source_label(args.dataset_source)
    dataset_license = _safe_source_label(args.dataset_license)
    feature_schema_hash = sha256("\n".join(features).encode("utf-8"))
    default_target_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", args.target).strip("._-") or "target"
    model_slug = args.model_name or f"{args.algorithm}_{default_target_slug}_{dataset_hash[:10]}"
    model_slug = f"{model_slug}_{artifact_hash[:10]}"
    if len(model_slug) > 180 or not re.fullmatch(r"[A-Za-z0-9._-]+", model_slug):
        raise ValueError("model name must use letters, numbers, dot, underscore, or hyphen and be at most 180 characters")
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / "model.onnx"
    temporary_model = model_path.with_suffix(model_path.suffix + ".tmp")
    temporary_model.write_bytes(artifact)
    temporary_model.replace(model_path)

    backend_root = Path(__file__).resolve().parents[1]
    source_hash = sha256(Path(__file__).read_bytes())
    dependency_file = backend_root / "pyproject.toml"

    manifest = {
        "schema_version": "savegeo-training-run/v1",
        "training_mode": "grouped_cross_validation",
        "task": "regression",
        "algorithm": args.algorithm,
        "parameters": {"seed": args.seed, "trees": args.trees, "max_depth": args.max_depth,
                       "minimum_r2": args.minimum_r2,
                       "max_baseline_rmse_ratio": args.max_baseline_rmse_ratio,
                       "fold_count": split_count},
        "dataset": {"name": source.name, "sha256": dataset_hash, "row_count": len(frame),
                    "feature_columns": features, "target_column": args.target,
                    "target_unit": args.target_unit,
                    "source": dataset_source,
                    "license": dataset_license,
                    "group_column": args.group, "group_count": len(unique_groups),
                    "group_ids_sha256": group_hash},
        "artifact": {"file": model_path.name, "sha256": artifact_hash, "format": "onnx"},
        "validation": {"method": "GroupKFold", "metrics": cv, "baseline_metrics": baseline_cv, "folds": fold_metrics,
                       "interpretation": "Out-of-fold agreement on supplied dataset; source/field representativeness must be reviewed separately."},
        "quality_preflight": preflight,
        "code_revision": _git_revision(),
        "code_dirty": _git_dirty(),
        "training_script_sha256": source_hash,
        "dependency_manifest_sha256": sha256(dependency_file.read_bytes()) if dependency_file.is_file() else None,
        "runtime_versions": _versions(),
        "resolved_distributions": _resolved_distributions(),
        "dvc_revision": os.getenv("DVC_REV"),
    }
    metrics = {"cv": cv, "baseline_cv": baseline_cv, "quality_preflight": preflight,
               "folds": fold_metrics, "n_samples": len(frame),
               "n_independent_groups": len(unique_groups)}
    _write_json(out / "manifest.json", manifest)
    _write_json(out / "metrics.json", metrics)

    _write_json(out / "mlflow-run.json", {"enabled": False, "run_id": None, "experiment_id": None})
    if args.mlflow:
        tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "").strip()
        if not tracking_uri:
            raise ValueError("Set MLFLOW_TRACKING_URI before using --mlflow")
        try:
            import mlflow
        except ImportError as exc:
            raise RuntimeError("Install the backend mlops extra to use MLflow tracking") from exc
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT_NAME", "savegeo-reproducible-training"))
        with mlflow.start_run(run_name=model_slug) as run:
            mlflow.log_params({"algorithm": args.algorithm, "seed": args.seed, "trees": args.trees,
                               "max_depth": args.max_depth or "unbounded", "fold_count": split_count,
                               "minimum_r2": args.minimum_r2,
                               "max_baseline_rmse_ratio": args.max_baseline_rmse_ratio,
                               "dataset_sha256": dataset_hash, "dataset_rows": len(frame),
                               "dataset_source": dataset_source or "unspecified",
                               "dataset_license": dataset_license or "unspecified",
                               "target_column": args.target, "target_unit": args.target_unit,
                               "group_column": args.group, "feature_count": len(features),
                               "feature_schema_sha256": feature_schema_hash,
                               "group_count": len(unique_groups), "code_revision": manifest["code_revision"] or "unknown"})
            mlflow.log_metrics({key: value for key, value in cv.items() if value is not None})
            mlflow.log_metrics({f"baseline_{key}": value for key, value in baseline_cv.items() if value is not None})
            _write_json(out / "mlflow-run.json", {"enabled": True,
                                                    "tracking_endpoint": _safe_tracking_endpoint(tracking_uri),
                                                    "run_id": run.info.run_id,
                                                    "experiment_id": run.info.experiment_id})
            mlflow.log_artifacts(str(out))

    if args.register:
        from app.core.config import get_settings
        from app.db.models.uploaded_model import UploadedModel
        from app.db.session import SessionLocal

        model_root = get_settings().model_path
        model_root.mkdir(parents=True, exist_ok=True)
        registered_path = model_root / f"{model_slug}.onnx"
        if registered_path.is_symlink():
            raise ValueError("Content-addressed model path must not be a symbolic link")
        if registered_path.is_file() and sha256(registered_path.read_bytes()) != artifact_hash:
            raise ValueError("Content-addressed model path already contains a different artifact")
        with SessionLocal() as db:
            existing = db.query(UploadedModel).filter_by(name=model_slug).first()
            if existing and existing.artifact_sha256 != artifact_hash:
                raise ValueError("Model registry name already belongs to a different artifact")
        if not registered_path.exists():
            temporary_registered = registered_path.with_suffix(".onnx.tmp")
            shutil.copy2(model_path, temporary_registered)
            temporary_registered.replace(registered_path)
        metadata = {"algorithm": args.algorithm, "version": f"1.0+{artifact_hash[:10]}",
                    "feature_names": features, "format": "onnx", "artifact_sha256": artifact_hash,
                    "training_manifest": manifest, "cv_metrics": cv, "n_samples": len(frame),
                    "training_source": "reproducible_cli", "model_name": model_slug,
                    "target_column": args.target, "target_unit": args.target_unit}
        tracking_file = out / "mlflow-run.json"
        if tracking_file.is_file():
            metadata["mlflow"] = json.loads(tracking_file.read_text(encoding="utf-8"))
        if existing is None:
            row = UploadedModel(
                name=model_slug,
                display_name=model_slug,
                model_type="carbon",
                algorithm=args.algorithm,
                filename=registered_path.name,
                filepath=str(registered_path.resolve()),
                file_size_kb=registered_path.stat().st_size / 1024,
                is_active=False,
                is_default=False,
                lifecycle_state="experimental",
                version=metadata["version"],
                description="Kandidat training grouped-CV; perlu review admin independen sebelum serving.",
                artifact_sha256=artifact_hash,
                feature_names=features,
                metadata_json=metadata,
                metrics={"cv_metrics": cv, "baseline_cv_metrics": baseline_cv,
                         "quality_preflight": preflight, "n_samples": len(frame)},
            )
            with SessionLocal() as db:
                db.add(row)
                db.commit()
    return {"model_name": model_slug, "metrics": metrics, "manifest": manifest,
            "registered": bool(args.register), "output_dir": str(out)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--target-unit", default="Mg C/ha")
    parser.add_argument("--dataset-source", default="", help="Public URL or internal source label; do not include credentials")
    parser.add_argument("--dataset-license", default="", help="Dataset license/terms label")
    parser.add_argument("--group", required=True)
    parser.add_argument("--features", required=True, help="Comma-separated numeric feature columns")
    parser.add_argument("--algorithm", choices=("random_forest", "extra_trees", "gradient_boosting"), default="random_forest")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--trees", type=int, default=160)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--minimum-r2", type=float, default=0.0)
    parser.add_argument("--max-baseline-rmse-ratio", type=float, default=1.0)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--output-dir", default="var/training_runs/candidate")
    parser.add_argument("--mlflow", nargs="?", const="true", default="false", choices=("true", "false"))
    parser.add_argument("--register", action="store_true", help="Copy to MODEL_DIR and register as inactive; never activates")
    args = parser.parse_args()
    args.mlflow = args.mlflow == "true"
    if args.seed < 0 or not 10 <= args.trees <= 5000 or not 1 <= args.max_depth <= 128:
        parser.error("seed/trees/max-depth are outside supported bounds")
    if not np.isfinite(args.minimum_r2) or not -1.0 <= args.minimum_r2 <= 1.0:
        parser.error("minimum-r2 must be finite and between -1 and 1")
    if not np.isfinite(args.max_baseline_rmse_ratio) or not 0 < args.max_baseline_rmse_ratio <= 2:
        parser.error("max-baseline-rmse-ratio must be finite and in (0, 2]")
    return args


if __name__ == "__main__":
    try:
        print(json.dumps(train(parse_args()), ensure_ascii=False, indent=2, allow_nan=False))
    except Exception as exc:  # noqa: BLE001 - CLI emits a concise, actionable error
        print(f"Training failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
