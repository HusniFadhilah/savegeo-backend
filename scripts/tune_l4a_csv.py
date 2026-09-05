from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold, KFold, cross_validate, train_test_split
from sklearn.compose import TransformedTargetRegressor

from scripts.train_gedi_l4a_direct_model import FEATURE_NAMES, assign_grid_id


def catalogue(seed: int):
    models = {
        "rf_fast": RandomForestRegressor(
            n_estimators=90, max_depth=20, min_samples_leaf=2, max_features=0.9, random_state=seed, n_jobs=-1
        ),
        "et_fast": ExtraTreesRegressor(
            n_estimators=120, max_depth=None, min_samples_leaf=1, max_features=0.95, random_state=seed, n_jobs=-1
        ),
        "hgb_fast": HistGradientBoostingRegressor(
            max_iter=160, learning_rate=0.05, max_leaf_nodes=63, l2_regularization=0.01, random_state=seed
        ),
    }
    try:
        from xgboost import XGBRegressor

        models["xgb"] = XGBRegressor(
            n_estimators=260,
            max_depth=5,
            learning_rate=0.035,
            subsample=0.85,
            colsample_bytree=0.9,
            min_child_weight=3,
            objective="reg:squarederror",
            random_state=seed,
            n_jobs=-1,
        )
    except Exception:
        pass
    try:
        from lightgbm import LGBMRegressor

        models["lgbm"] = LGBMRegressor(
            n_estimators=320,
            learning_rate=0.03,
            num_leaves=31,
            min_child_samples=12,
            subsample=0.85,
            colsample_bytree=0.9,
            random_state=seed,
            n_jobs=-1,
            verbosity=-1,
        )
    except Exception:
        pass
    try:
        from catboost import CatBoostRegressor

        models["catboost"] = CatBoostRegressor(
            iterations=320,
            depth=6,
            learning_rate=0.035,
            l2_leaf_reg=4,
            loss_function="RMSE",
            random_seed=seed,
            verbose=False,
            allow_writing_files=False,
            thread_count=-1,
        )
    except Exception:
        pass

    for name, model in list(models.items()):
        if name not in {"rf_fast", "et_fast"}:
            models[f"log_{name}"] = TransformedTargetRegressor(
                regressor=model, func=np.log1p, inverse_func=np.expm1
            )
    return models


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cv-folds", type=int, default=5)
    parser.add_argument("--grid-deg", type=float, default=1.0)
    args = parser.parse_args()

    df = pd.read_csv(args.csv_path)
    df = assign_grid_id(df, args.grid_deg)
    x = df[FEATURE_NAMES].values
    y = df["carbon_stock"].values
    groups = df["grid_id"].values
    x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2, random_state=args.seed)
    print(f"samples={len(df)} features={len(FEATURE_NAMES)} groups={df['grid_id'].nunique()} target_mean={y.mean():.2f}")

    rows = []
    for name, model in catalogue(args.seed).items():
        model.fit(x_train, y_train)
        pred = model.predict(x_test)
        cv = KFold(n_splits=args.cv_folds, shuffle=True, random_state=args.seed)
        cv_scores = cross_validate(model, x, y, cv=cv, scoring=["r2", "neg_root_mean_squared_error", "neg_mean_absolute_error"])
        spatial_r2 = np.nan
        if len(np.unique(groups)) >= args.cv_folds:
            gkf = GroupKFold(n_splits=args.cv_folds)
            spatial_scores = cross_validate(model, x, y, groups=groups, cv=gkf, scoring=["r2"])
            spatial_r2 = float(np.mean(spatial_scores["test_r2"]))
        row = {
            "name": name,
            "test_r2": float(r2_score(y_test, pred)),
            "test_rmse": float(np.sqrt(mean_squared_error(y_test, pred))),
            "test_mae": float(mean_absolute_error(y_test, pred)),
            "cv_r2": float(np.mean(cv_scores["test_r2"])),
            "spatial_r2": spatial_r2,
        }
        rows.append(row)
        print(
            f"{name}: test_R2={row['test_r2']:.4f} cv_R2={row['cv_r2']:.4f} "
            f"spatial_R2={row['spatial_r2']:.4f} rmse={row['test_rmse']:.2f}"
        )
    best = max(rows, key=lambda item: item["spatial_r2"] if np.isfinite(item["spatial_r2"]) else item["cv_r2"])
    print(f"best={best['name']} selection_R2={best['spatial_r2']:.4f} random_CV_R2={best['cv_r2']:.4f}")


if __name__ == "__main__":
    main()
