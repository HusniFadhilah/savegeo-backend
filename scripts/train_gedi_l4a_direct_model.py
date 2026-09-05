"""Train and register a direct GEDI L4A Monthly carbon model.

This script trains from GEDI L4A footprint/vector observations, not from the
existing GEDI L4B model. The monthly raster product is extremely sparse for
random pixel sampling, so the correct path is:

1. Read `LARSE/GEDI/GEDI04_A_002_INDEX`.
2. Load indexed L4A footprint tables for the requested year.
3. Keep valid footprints (`agbd > 0`, `l4_quality_flag == 1`,
   `degrade_flag == 0`).
4. Sample Sentinel-2 + SRTM predictor bands at those footprint locations.
5. Train/evaluate several regressors with KFold and spatial GroupKFold.
6. Register the best model only when validation R2 reaches the threshold.

Usage:
    python -m scripts.train_gedi_l4a_direct_model
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold, KFold, cross_validate, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.compose import TransformedTargetRegressor
from sklearn.preprocessing import StandardScaler

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal


DATASET_KEY = "_".join(("GEDI", "L4A", "MONTHLY"))
DATASET_ID = "/".join(("LARSE", "GEDI", "GEDI04_A_002_MONTHLY"))
INDEX_ID = "/".join(("LARSE", "GEDI", "GEDI04_A_002_INDEX"))
MODEL_SLUG = "_".join(("gedi", "l4a", "monthly", "direct", "s2", "dem", "landcover", "2021"))
DISPLAY_NAME = "GEDI L4A Monthly Direct S2 DEM Landcover 2021"

ISLAND_BBOXES = {
    "sumatra": [95.0, -6.0, 106.0, 6.0],
    "jawa": [105.0, -9.0, 115.0, -5.5],
    "kalimantan": [108.0, -4.5, 119.0, 4.5],
    "sulawesi": [118.0, -6.0, 125.5, 2.0],
    "bali_nusa_tenggara": [114.0, -11.0, 125.5, -7.5],
    "maluku": [124.0, -8.5, 135.0, 3.0],
    "papua": [130.0, -9.5, 141.5, 0.5],
}

S2_BANDS = ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
S2_INDICES = ["NDVI", "NDWI", "NDMI", "NBR", "NDRE", "EVI", "SAVI", "BSI", "brightness"]
FEATURE_NAMES = S2_BANDS + S2_INDICES + [
    "elevation",
    "slope",
    "aspect",
    "landcover",
    "forest_mask",
    "NDVI_x_elevation",
    "NDMI_x_slope",
    "forest_mask_x_NDVI",
    "B8_x_B11",
]


def _init_ee() -> None:
    import ee

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    key_file = os.getenv("GEE_KEY_FILE")
    service_account = os.getenv("GEE_SERVICE_ACCOUNT")
    project_id = os.getenv("GEE_PROJECT_ID") or None
    if key_file and service_account and Path(key_file).exists():
        credentials = ee.ServiceAccountCredentials(service_account, key_file)
        ee.Initialize(credentials, project=project_id)
    else:
        ee.Initialize(project=project_id)
    print("Earth Engine initialized", flush=True)


def _add_s2_indices(img):
    import ee

    ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndwi = img.normalizedDifference(["B3", "B8"]).rename("NDWI")
    ndmi = img.normalizedDifference(["B8", "B11"]).rename("NDMI")
    nbr = img.normalizedDifference(["B8", "B12"]).rename("NBR")
    ndre = img.normalizedDifference(["B8A", "B5"]).rename("NDRE")
    evi = img.expression(
        "2.5*((NIR-RED)/(NIR+6*RED-7.5*BLUE+1))",
        {"NIR": img.select("B8"), "RED": img.select("B4"), "BLUE": img.select("B2")},
    ).rename("EVI")
    savi = img.expression(
        "((NIR-RED)/(NIR+RED+0.5))*1.5",
        {"NIR": img.select("B8"), "RED": img.select("B4")},
    ).rename("SAVI")
    bsi = img.expression(
        "((SWIR1+RED)-(NIR+BLUE))/((SWIR1+RED)+(NIR+BLUE))",
        {"SWIR1": img.select("B11"), "RED": img.select("B4"), "NIR": img.select("B8"), "BLUE": img.select("B2")},
    ).rename("BSI")
    brightness = img.select(["B2", "B3", "B4"]).reduce(ee.Reducer.mean()).rename("brightness")
    return img.addBands([ndvi, ndwi, ndmi, nbr, ndre, evi, savi, bsi, brightness])


def build_s2_dem_landcover_stack(roi, year: int, cloud_threshold: int):
    import ee

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi)
        .filterDate(f"{year}-01-01", f"{year}-12-31")
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_threshold))
        .median()
        .multiply(0.0001)
        .select(S2_BANDS)
    )
    stack = _add_s2_indices(s2)

    dem = ee.Image("USGS/SRTMGL1_003").select("elevation")
    terrain = ee.Terrain.products(dem)
    elevation = terrain.select("elevation").rename("elevation")
    slope = terrain.select("slope").rename("slope")
    aspect = terrain.select("aspect").rename("aspect")

    landcover = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map").rename("landcover")
    forest_mask = landcover.eq(10).rename("forest_mask")

    stack = stack.addBands([elevation, slope, aspect, landcover, forest_mask])
    return stack.addBands(
        [
            stack.select("NDVI").multiply(stack.select("elevation")).rename("NDVI_x_elevation"),
            stack.select("NDMI").multiply(stack.select("slope")).rename("NDMI_x_slope"),
            stack.select("forest_mask").multiply(stack.select("NDVI")).rename("forest_mask_x_NDVI"),
            stack.select("B8").multiply(stack.select("B11")).rename("B8_x_B11"),
        ]
    ).select(FEATURE_NAMES)


def assign_grid_id(df: pd.DataFrame, grid_deg: float) -> pd.DataFrame:
    out = df.copy()
    gx = np.floor(out["lon"].values / grid_deg).astype(int)
    gy = np.floor(out["lat"].values / grid_deg).astype(int)
    out["grid_id"] = [f"{x}_{y}" for x, y in zip(gx, gy)]
    return out


def _table_ids_for_island(island: str, year: int, max_tables: int, seed: int, table_order: str) -> list[str]:
    import ee

    region = ee.Geometry.Rectangle(ISLAND_BBOXES[island])
    idx = (
        ee.FeatureCollection(INDEX_ID)
        .filterBounds(region)
        .filter(ee.Filter.gte("time_start", f"{year}-01-01"))
        .filter(ee.Filter.lt("time_start", f"{year + 1}-01-01"))
    )
    if table_order == "random":
        idx = idx.randomColumn("table_rand", seed).sort("table_rand")
    else:
        idx = idx.sort("time_start")
    return idx.aggregate_array("table_id").slice(0, max_tables).getInfo()


def sample_l4a_direct(
    year: int,
    samples: int,
    scale: int,
    cloud_threshold: int,
    max_tables_per_island: int,
    max_carbon: float | None,
    table_order: str,
    seed: int,
    islands: list[str],
) -> pd.DataFrame:
    import ee

    rows: list[dict] = []
    per_island = max(1, math.ceil(samples / len(islands)))

    for island in islands:
        region = ee.Geometry.Rectangle(ISLAND_BBOXES[island])
        table_ids = _table_ids_for_island(island, year, max_tables_per_island, seed, table_order)
        if not table_ids:
            print(f"{island}: no L4A indexed tables", flush=True)
            continue
        print(f"{island}: loading {len(table_ids)} L4A tables, target up to {per_island} clean samples", flush=True)

        l4a = ee.FeatureCollection([ee.FeatureCollection(t).filterBounds(region) for t in table_ids]).flatten()
        valid = (
            l4a.filter(ee.Filter.gt("agbd", 0))
            .filter(ee.Filter.eq("l4_quality_flag", 1))
            .filter(ee.Filter.eq("degrade_flag", 0))
            .randomColumn("sample_rand", seed)
            .sort("sample_rand")
            .limit(per_island * 2)
            .map(
                lambda f, island_name=island: f.set(
                    {
                        "carbon_stock": ee.Number(f.get("agbd")).multiply(0.47),
                        "island": island_name,
                    }
                )
            )
        )

        stack = build_s2_dem_landcover_stack(region, year, cloud_threshold)
        sampled = stack.sampleRegions(
            collection=valid,
            properties=["carbon_stock", "island"],
            scale=scale,
            geometries=True,
            tileScale=16,
        )
        got = sampled.limit(per_island * 2).getInfo().get("features", [])
        island_rows = []
        for feat in got:
            props = feat.get("properties", {})
            geom = feat.get("geometry", {}) or {}
            coords = geom.get("coordinates") or [None, None]
            props["lon"] = coords[0]
            props["lat"] = coords[1]
            island_rows.append(props)
        print(f"{island}: tables={len(table_ids)} sampled={len(island_rows)}", flush=True)
        rows.extend(island_rows)

    df = pd.DataFrame(rows)
    required = FEATURE_NAMES + ["carbon_stock", "lon", "lat", "island"]
    for col in required:
        if col not in df.columns:
            df[col] = np.nan
    clean = df[required].dropna().copy()
    numeric_cols = FEATURE_NAMES + ["carbon_stock", "lon", "lat"]
    clean[numeric_cols] = clean[numeric_cols].apply(pd.to_numeric, errors="coerce")
    clean = clean.dropna(subset=numeric_cols)
    clean = clean[np.isfinite(clean[numeric_cols].values).all(axis=1)]
    clean = clean[clean["carbon_stock"] > 0].reset_index(drop=True)
    if max_carbon:
        clean = clean[clean["carbon_stock"] <= max_carbon].reset_index(drop=True)
    if len(clean) > samples:
        clean = clean.sample(n=samples, random_state=seed).reset_index(drop=True)
    return clean


def model_catalogue(seed: int):
    rf = RandomForestRegressor(
        n_estimators=420, max_depth=24, min_samples_leaf=2, max_features=0.85, random_state=seed, n_jobs=-1
    )
    et = ExtraTreesRegressor(
        n_estimators=520, max_depth=None, min_samples_leaf=2, max_features=0.9, random_state=seed, n_jobs=-1
    )
    hgb = HistGradientBoostingRegressor(
        max_iter=420, learning_rate=0.035, max_leaf_nodes=63, l2_regularization=0.02, random_state=seed
    )
    return {
        "ridge": make_pipeline(StandardScaler(), Ridge(alpha=10.0, random_state=seed)),
        "random_forest": rf,
        "extra_trees": et,
        "hist_gradient_boosting": hgb,
        "log_random_forest": TransformedTargetRegressor(regressor=rf, func=np.log1p, inverse_func=np.expm1),
        "log_extra_trees": TransformedTargetRegressor(regressor=et, func=np.log1p, inverse_func=np.expm1),
        "log_hist_gradient_boosting": TransformedTargetRegressor(regressor=hgb, func=np.log1p, inverse_func=np.expm1),
    }


def evaluate_models(df: pd.DataFrame, cv_folds: int, seed: int):
    X = df[FEATURE_NAMES].values
    y = df["carbon_stock"].values
    groups = df["grid_id"].values
    x_train, x_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=seed)
    rows = []
    fitted = {}
    for name, estimator in model_catalogue(seed).items():
        estimator.fit(x_train, y_train)
        pred = estimator.predict(x_test)
        kfold = KFold(n_splits=cv_folds, shuffle=True, random_state=seed)
        kfold_scores = cross_validate(
            estimator,
            X,
            y,
            cv=kfold,
            scoring=["r2", "neg_root_mean_squared_error", "neg_mean_absolute_error"],
        )
        spatial = None
        if len(np.unique(groups)) >= cv_folds:
            gkf = GroupKFold(n_splits=cv_folds)
            spatial_scores = cross_validate(
                estimator,
                X,
                y,
                groups=groups,
                cv=gkf,
                scoring=["r2", "neg_root_mean_squared_error", "neg_mean_absolute_error"],
            )
            spatial = {
                "r2_mean": float(np.mean(spatial_scores["test_r2"])),
                "r2_std": float(np.std(spatial_scores["test_r2"])),
                "rmse_mean": float(np.mean(-spatial_scores["test_neg_root_mean_squared_error"])),
                "mae_mean": float(np.mean(-spatial_scores["test_neg_mean_absolute_error"])),
                "n_folds": cv_folds,
                "n_groups": int(len(np.unique(groups))),
            }

        cv = {
            "r2_mean": float(np.mean(kfold_scores["test_r2"])),
            "r2_std": float(np.std(kfold_scores["test_r2"])),
            "rmse_mean": float(np.mean(-kfold_scores["test_neg_root_mean_squared_error"])),
            "mae_mean": float(np.mean(-kfold_scores["test_neg_mean_absolute_error"])),
            "n_folds": cv_folds,
        }
        row = {
            "algorithm": name,
            "test_r2": float(r2_score(y_test, pred)),
            "test_rmse": float(np.sqrt(mean_squared_error(y_test, pred))),
            "test_mae": float(mean_absolute_error(y_test, pred)),
            "cv_metrics": cv,
            "spatial_cv_metrics": spatial,
            "selection_r2": float((spatial or cv)["r2_mean"]),
        }
        print(f"{name}: test_R2={row['test_r2']:.4f} cv_R2={cv['r2_mean']:.4f} spatial_R2={row['selection_r2']:.4f}")
        rows.append(row)
        fitted[name] = estimator
    best = max(rows, key=lambda r: r["selection_r2"])
    return best, rows, fitted[best["algorithm"]]


def train_gee_native_classifier(df: pd.DataFrame, algorithm: str, seed: int):
    import ee

    features = []
    for _, row in df.iterrows():
        props = {col: float(row[col]) for col in FEATURE_NAMES}
        props["carbon_stock"] = float(row["carbon_stock"])
        features.append(ee.Feature(None, props))
    fc = ee.FeatureCollection(features)
    if algorithm == "hist_gradient_boosting":
        clf = ee.Classifier.smileGradientTreeBoost(
            numberOfTrees=220,
            shrinkage=0.045,
            samplingRate=0.75,
            maxNodes=2000,
            loss="LeastSquares",
            seed=seed,
        ).setOutputMode("REGRESSION")
    else:
        clf = ee.Classifier.smileRandomForest(numberOfTrees=220, seed=seed).setOutputMode("REGRESSION")
    return clf.train(features=fc, classProperty="carbon_stock", inputProperties=FEATURE_NAMES).serialize()


def register_model(model_path: Path, clf_path: Path, metadata: dict, metrics: dict) -> None:
    db = SessionLocal()
    try:
        row = db.query(UploadedModel).filter_by(name=MODEL_SLUG).first()
        if row is None:
            row = UploadedModel(name=MODEL_SLUG, model_type="carbon")
            db.add(row)
        row.display_name = DISPLAY_NAME
        row.algorithm = metadata["algorithm"]
        row.filename = model_path.name
        row.filepath = str(model_path.resolve())
        row.file_size_kb = round(model_path.stat().st_size / 1024, 2)
        row.is_active = True
        row.is_default = False
        row.is_legacy = False
        row.description = "Direct GEDI L4A Monthly footprint-trained carbon model with spatial validation."
        row.version = metadata["version"]
        row.metrics = metrics
        row.feature_names = FEATURE_NAMES
        row.metadata_json = metadata
        db.commit()
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2021)
    parser.add_argument("--samples", type=int, default=1200)
    parser.add_argument("--scale", type=int, default=30)
    parser.add_argument("--cloud-threshold", type=int, default=55)
    parser.add_argument("--max-tables-per-island", type=int, default=30)
    parser.add_argument("--max-carbon", type=float, default=350.0)
    parser.add_argument("--table-order", choices=["time", "random"], default="time")
    parser.add_argument("--cv-folds", type=int, default=5)
    parser.add_argument("--grid-deg", type=float, default=1.0)
    parser.add_argument("--min-r2", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--islands", nargs="+", default=list(ISLAND_BBOXES.keys()), choices=list(ISLAND_BBOXES.keys()))
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    _init_ee()

    settings = get_settings()
    out_dir = settings.model_path
    out_dir.mkdir(parents=True, exist_ok=True)
    experiment_dir = Path("var") / "training_runs" / MODEL_SLUG
    experiment_dir.mkdir(parents=True, exist_ok=True)

    df = sample_l4a_direct(
        year=args.year,
        samples=args.samples,
        scale=args.scale,
        cloud_threshold=args.cloud_threshold,
        max_tables_per_island=args.max_tables_per_island,
        max_carbon=args.max_carbon,
        table_order=args.table_order,
        seed=args.seed,
        islands=args.islands,
    )
    if len(df) < 50:
        raise RuntimeError(f"Only {len(df)} clean GEDI L4A samples; cannot train.")
    df = assign_grid_id(df, args.grid_deg)
    df.to_csv(experiment_dir / "gedi_l4a_direct_clean.csv", index=False)
    print(f"clean samples={len(df)} groups={df['grid_id'].nunique()} target_mean={df['carbon_stock'].mean():.2f}")

    best, all_rows, estimator = evaluate_models(df, args.cv_folds, args.seed)
    (experiment_dir / "metrics.json").write_text(json.dumps(all_rows, indent=2), encoding="utf-8")
    if best["selection_r2"] < args.min_r2:
        raise RuntimeError(f"Best direct L4A R2={best['selection_r2']:.4f}, below threshold {args.min_r2}.")

    model_path = out_dir / f"{MODEL_SLUG}.pkl"
    json_path = out_dir / f"{MODEL_SLUG}.json"
    clf_path = out_dir / f"{MODEL_SLUG}.gee_clf.json"

    joblib.dump({"model": estimator, "scaler": None, "feature_names": FEATURE_NAMES}, model_path)
    serialized = train_gee_native_classifier(df, best["algorithm"], args.seed)
    clf_path.write_text(serialized, encoding="utf-8")

    now = datetime.now(UTC).isoformat()
    metadata = {
        "model_name": MODEL_SLUG,
        "display_name": DISPLAY_NAME,
        "algorithm": best["algorithm"],
        "version": f"gedi-l4a-direct-{args.year}",
        "created_at": now,
        "trained_at": now,
        "target_dataset_key": DATASET_KEY,
        "reference_dataset": DATASET_KEY,
        "compatible_reference_datasets": [DATASET_KEY],
        "reference_gee_id": DATASET_ID,
        "reference_source": "LARSE/GEDI/GEDI04_A_002 indexed footprint tables",
        "reference_band": "agbd",
        "reference_transform": "agbd * 0.47",
        "target_pool": "aboveground_biomass_carbon",
        "target_unit": "Mg C/ha",
        "feature_stack": "s2_dem_landcover",
        "feature_set_name": "s2_dem_landcover",
        "feature_names": FEATURE_NAMES,
        "feature_count": len(FEATURE_NAMES),
        "n_features": len(FEATURE_NAMES),
        "sample_count": len(df),
        "n_samples": len(df),
        "training_year": args.year,
        "training_scale_m": args.scale,
        "table_order": args.table_order,
        "quality_filter": "agbd > 0 AND l4_quality_flag == 1 AND degrade_flag == 0",
        "max_carbon_mg_cha": args.max_carbon,
        "cv_metrics": best["cv_metrics"],
        "spatial_cv_metrics": best["spatial_cv_metrics"],
        "train_metrics": {"r2": best["test_r2"], "rmse": best["test_rmse"], "mae": best["test_mae"]},
        "gee_deployable": True,
        "gee_algorithm_type": "native_classifier",
        "gee_classifier_path": clf_path.name,
        "gee_feature_names": FEATURE_NAMES,
        "is_production_ready": True,
        "spatial_split_method": f"GroupKFold(grid_id, {args.grid_deg} deg)",
        "limitations": [
            "Trained from GEDI L4A footprint observations, not field plot measurements.",
            "Spatial CV should be trusted over random split metrics.",
            "GEDI L4A footprints are sparse and not wall-to-wall; performance depends on representativeness of sampled tracks.",
        ],
        "experiment_dir": str(experiment_dir),
        "experiment_results": all_rows,
    }
    json_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    metrics = {
        "train_metrics": metadata["train_metrics"],
        "cv_metrics": best["spatial_cv_metrics"] or best["cv_metrics"],
        "n_samples": len(df),
        "n_features": len(FEATURE_NAMES),
        "trained_at": now,
        "scaled_features": False,
    }
    register_model(model_path, clf_path, metadata, metrics)
    print(f"registered {MODEL_SLUG} algorithm={best['algorithm']} R2={metrics['cv_metrics']['r2_mean']:.4f}")


if __name__ == "__main__":
    main()
