"""Audit gaps, compare regressors, and deploy frozen regional reference models.

Run: python -m scripts.train_carbon_reference_models
Artifacts and source samples are retained in var/; reruns reuse samples and
upsert stable model names. Metrics describe raster agreement, not field accuracy.
"""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import ee
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal
from app.registries.carbon_dataset_registry import CARBON_DATASET_REGISTRY, CARBON_ARCGIS_REGISTRY, get_dataset_meta, load_carbon_reference_ee
from app.services.carbon_saved_models import FEATURES, predictors, tree_string
from app.services.carbon_service import get_carbon_dataset_list
from app.services.gee_service import _init_from_key_file


BBOX = [108.0, -8.3, 112.0, -6.0]
ROOT = Path("var/training_runs/carbon_reference_20261009")


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def metrics(observed, predicted):
    return {"r2": float(r2_score(observed, predicted)), "rmse": float(np.sqrt(mean_squared_error(observed, predicted))),
            "mae": float(mean_absolute_error(observed, predicted)), "bias": float(np.mean(predicted - observed))}


def sample_group(keys, year, count):
    cache = ROOT / f"samples_{year}_{hashlib.sha256(','.join(keys).encode()).hexdigest()[:12]}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    region = ee.Geometry.Rectangle(BBOX, proj="EPSG:4326", geodesic=False)
    names = FEATURES + (["s1_vv", "s1_vh"] if year >= 2015 else [])
    stack = predictors(region, year, names).addBands(ee.Image.pixelLonLat())
    # Sampling predictor masks only prevents one dataset's missing values from
    # removing usable training data in other datasets. Sentinel nodata never trains.
    for key in keys:
        if key in CARBON_DATASET_REGISTRY:
            label = load_carbon_reference_ee(key, year).rename(key).unmask(-999999)
            stack = stack.addBands(label)
    scale = max(300, max(get_dataset_meta(key)["resolution"] for key in keys))
    features = stack.sample(region=region, scale=scale, numPixels=count, seed=42,
                            geometries=False, tileScale=4).getInfo()["features"]
    rows = [feature["properties"] for feature in features]
    dump(cache, {"rows": rows, "feature_names": names, "scale": scale})
    return {"rows": rows, "feature_names": names, "scale": scale}


def external_labels(frame, key, year):
    from app.providers.local_raster_provider import GridSpec, load_carbon_reference_to_grid, load_raster_to_grid
    from pyproj import Transformer
    from rasterio.transform import rowcol

    if key in CARBON_ARCGIS_REGISTRY:
        import requests

        values = np.full(len(frame), np.nan)
        url = CARBON_ARCGIS_REGISTRY[key]["arcgis_service_url"] + "/getSamples"
        for start in range(0, len(frame), 200):
            points = frame.iloc[start:start + 200][["longitude", "latitude"]].to_numpy().tolist()
            response = requests.post(url, data={"f": "json", "geometryType": "esriGeometryMultipoint",
                                                "geometry": json.dumps({"points": points, "spatialReference": {"wkid": 4326}}),
                                                "returnFirstValueOnly": "true"}, timeout=60)
            response.raise_for_status()
            result = response.json()
            if result.get("error"):
                raise ValueError("ArcGIS label sampling failed: " + str(result["error"]))
            for sample in result.get("samples", []):
                try:
                    values[start + int(sample["locationId"])] = float(sample["value"])
                except (KeyError, TypeError, ValueError):
                    continue
        return values
    grid = GridSpec(tuple(BBOX), resolution_m=max(300, get_dataset_meta(key)["resolution"]))
    if key == "CHLORIS_AGB_STOCK":
        from app.services.chloris_service import resolve_chloris_download, _planetary_computer_chloris_item

        source = resolve_chloris_download(year=year)
        if source.metadata.get("source") == "planetary_computer_stac":
            _, pixel_area_ha = _planetary_computer_chloris_item(year)
            raw = load_raster_to_grid(source.url, grid, src_nodata=2147483647, resampling="nearest")
            if raw is None:
                raise ValueError("Public Chloris source could not be sampled.")
            labels = raw * 0.47 / pixel_area_ha
        else:
            labels, _ = load_carbon_reference_to_grid(key, grid, year=year)
    else:
        labels, _ = load_carbon_reference_to_grid(key, grid, year=year)
    transformer = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    xs, ys = transformer.transform(frame.longitude.to_numpy(), frame.latitude.to_numpy())
    rr, cc = rowcol(grid.transform, xs, ys)
    values = np.full(len(frame), np.nan)
    for i, (r, c) in enumerate(zip(rr, cc)):
        if 0 <= r < grid.shape[0] and 0 <= c < grid.shape[1]:
            values[i] = labels[r, c]
    return values


def evaluate(frame, key, names):
    clean = frame.replace(-999999, np.nan).dropna(subset=names + [key, "longitude", "latitude"])
    if len(clean) < 200:
        raise ValueError(f"Only {len(clean)} valid samples; minimum 200.")
    x = clean[names].to_numpy(dtype=float)
    y = clean[key].to_numpy(dtype=float)
    if not np.isfinite(y).all() or np.std(y) < 1e-8:
        raise ValueError("Reference target is empty or constant.")
    gx = np.floor(clean.longitude.to_numpy() / 0.1).astype(int)
    gy = np.floor(clean.latitude.to_numpy() / 0.1).astype(int)
    groups = gx * 10000 + gy
    folds = list(GroupKFold(n_splits=3).split(x, y, groups))
    candidates = {
        "random_forest": RandomForestRegressor(n_estimators=80, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=-1),
        "extra_trees": ExtraTreesRegressor(n_estimators=80, max_depth=12, min_samples_leaf=4, random_state=42, n_jobs=-1),
        "gradient_boosting": GradientBoostingRegressor(n_estimators=80, max_depth=3, min_samples_leaf=5, learning_rate=0.05, random_state=42),
    }
    scores = {}
    for algorithm, estimator in candidates.items():
        predicted = np.zeros(len(y))
        for training, validation in folds:
            estimator.fit(x[training], y[training])
            predicted[validation] = np.maximum(0, estimator.predict(x[validation]))
        scores[algorithm] = metrics(y, predicted)
        estimator.fit(x, y)
    return clean, candidates, scores


def deploy(key, year, clean, names, algorithm, estimator, score, all_scores, sample_scale):
    slug = f"reference_{key.lower()}_{year}_{algorithm}_java_v1"
    settings = get_settings()
    output = settings.model_path
    output.mkdir(parents=True, exist_ok=True)
    trees = estimator.estimators_.ravel() if algorithm == "gradient_boosting" else estimator.estimators_
    artifact = {"algorithm": algorithm, "trees": [tree_string(tree, names) for tree in trees]}
    if algorithm == "gradient_boosting":
        artifact.update(initial_prediction=float(estimator.init_.constant_[0, 0]), learning_rate=estimator.learning_rate)
    # Check the frozen model in EE before exposing it in the registry.
    probe = clean.iloc[:12]
    check_image = ee.Image.constant(0).rename("placeholder")
    points = []
    for i, (_, row) in enumerate(probe.iterrows()):
        points.append(ee.Feature(None, {**{name: float(np.float32(row[name])) for name in names}, "sample_id": i}))
    if algorithm == "gradient_boosting":
        fc = ee.FeatureCollection(points)
        initial = artifact["initial_prediction"]
        fc = fc.map(lambda f: f.set("prediction", initial))
        for tree in artifact["trees"]:
            fc = fc.classify(ee.Classifier.decisionTree(tree).setOutputMode("REGRESSION"), "term")
            fc = fc.map(lambda f: f.set("prediction", ee.Number(f.get("prediction")).add(ee.Number(f.get("term")).multiply(artifact["learning_rate"]))))
    else:
        fc = ee.FeatureCollection(points).classify(ee.Classifier.decisionTreeEnsemble(artifact["trees"]).setOutputMode("REGRESSION"), "prediction")
    values = fc.getInfo()["features"]
    values = sorted(values, key=lambda f: f["properties"]["sample_id"])
    predicted = np.maximum(0, [f["properties"]["prediction"] for f in values])
    parity = float(np.max(np.abs(predicted - np.maximum(0, estimator.predict(probe[names].to_numpy())))))
    if parity > 1e-4:
        raise ValueError(f"Frozen EE prediction mismatch {parity}")
    del check_image
    meta = get_dataset_meta(key)
    artifact_path = output / f"{slug}.ensemble.json"
    dump(artifact_path, artifact)
    metadata = {
        "model_name": slug, "method": "saved_reference_ensemble", "provider": "builtin_gee", "builtin": True,
        "gee_deployable": True, "gee_algorithm_type": "native_classifier", "deployment_mode": "frozen_ensemble",
        "frozen_ensemble_file": artifact_path.name, "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "compatible_reference_datasets": [key], "target_dataset_key": key, "target_pool": meta["target_pool"],
        "target_unit": meta["unit"], "feature_names": names, "covariate_year": year, "reference_year": year,
        "training_bbox": BBOX, "time_series_supported": False, "accuracy_status": "local_field_validation_required",
        "preliminary": True, "training_mode": "reference_raster", "validation_type": "spatial_reference_holdout",
        "n_samples": len(clean), "validation_samples": len(clean), "spatial_block_degrees": 0.1,
        "validation_metrics": score, "candidate_metrics": all_scores, "sample_scale_m": sample_scale,
        "trained_at": datetime.now(UTC).isoformat(), "frozen_gee_parity_max_error": parity,
        "description": "Frozen regional regression, calibrated to the reference raster in Java. Three spatial folds; not independently field validated. Baseline/year and training domain are enforced.",
    }
    if key == "CHLORIS_AGB_STOCK":
        metadata.update(source_license="CC BY-NC-SA-4.0 (public Chloris reference)",
                        description="Regional model calibrated to PUBLIC Chloris 4633 m biomass, not the licensed high-resolution product. Non-commercial/share-alike reference license applies. Not independently field validated.")
    dump(output / f"{slug}.json", metadata)
    with SessionLocal() as db:
        row = db.query(UploadedModel).filter_by(name=slug).first()
        if row is None:
            row = UploadedModel(name=slug, model_type="carbon")
            db.add(row)
        row.display_name = f"{algorithm.replace('_', ' ').title()} · {meta['name']} · Jawa {year}"
        row.algorithm = algorithm
        row.filename = artifact_path.name
        row.filepath = str(artifact_path.resolve())
        row.file_size_kb = artifact_path.stat().st_size / 1024
        row.is_active = True
        row.is_default = False
        row.version = "1.0"
        row.description = metadata["description"]
        row.feature_names = names
        row.metadata_json = metadata
        row.metrics = {"cv_metrics": {"r2_mean": score["r2"], "rmse_mean": score["rmse"], "mae_mean": score["mae"]}, "n_samples": len(clean)}
        db.commit()
    return {"name": slug, "metrics": score, "parity_max_error": parity}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1800)
    parser.add_argument("--keys", nargs="*")
    args = parser.parse_args()
    settings = get_settings()
    _init_from_key_file(settings.gee_service_account, settings.gee_key_file)
    ee.data.setDeadline(120000)
    ROOT.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        catalog = get_carbon_dataset_list(db, require_model=False)
    dump(ROOT / "catalog_before.json", catalog)
    selected = [d for d in catalog if d["training_capable"] and
                (not d["compatible_models"] or CARBON_DATASET_REGISTRY.get(d["key"], {}).get("pool_analysis") or d["key"] == "GEDI_L4D")]
    if args.keys:
        selected = [d for d in catalog if d["key"] in args.keys]
    report = json.loads((ROOT / "report.json").read_text(encoding="utf-8")) if (ROOT / "report.json").exists() else {"datasets": {}}
    report.update({"training_bbox": BBOX, "validation": "three-fold spatial raster-reference agreement; not field accuracy",
              "untrainable": [{"key": d["key"], "reason": "Catalog training_capable=false; no verified training-label ingestion."}
                              for d in catalog if not d["training_capable"] and not d["compatible_models"]]})
    grouped = {}
    for dataset in selected:
        meta = get_dataset_meta(dataset["key"])
        year = int(meta.get("selection_year") or meta["year"])
        grouped.setdefault(year, []).append(dataset["key"])
    for year, keys in grouped.items():
        try:
            sampled = sample_group(keys, year, args.samples)
            frame = pd.DataFrame(sampled["rows"])
        except Exception as exc:
            for key in keys:
                report["datasets"][key] = {"status": "failed", "error": str(exc)}
            dump(ROOT / "report.json", report)
            print(f"Sampling {year} failed: {exc}", flush=True)
            continue
        for key in keys:
            try:
                current = frame.copy()
                if key not in CARBON_DATASET_REGISTRY:
                    current[key] = external_labels(frame, key, year)
                clean, candidates, scores = evaluate(current, key, sampled["feature_names"])
                clean.to_csv(ROOT / f"{key}_{year}.csv", index=False)
                best = max(scores, key=lambda a: scores[a]["r2"])
                if scores[best]["r2"] <= 0:
                    raise ValueError(f"No candidate beats mean predictor on spatial validation: {scores}")
                # AGB gets RF and boosting alternatives if both outperform the
                # constant baseline. Other targets get one best model only.
                algorithms = [best]
                if key in {"CTREES_AGB_100M", "ESA_CCI_BIOMASS_V7_COG", "GEDI_L4D"}:
                    algorithms = list(dict.fromkeys([best] + [a for a in ("random_forest", "gradient_boosting") if scores[a]["r2"] > 0]))
                deployed = [deploy(key, year, clean, sampled["feature_names"], a, candidates[a], scores[a], scores, sampled["scale"]) for a in algorithms]
                report["datasets"][key] = {"status": "deployed_local", "n_samples": len(clean), "candidates": scores, "models": deployed}
                print(key, len(clean), best, round(scores[best]["r2"], 3), "deployed", len(deployed), flush=True)
            except Exception as exc:
                report["datasets"][key] = {"status": "failed", "error": str(exc)}
                print(key, "FAILED", str(exc), flush=True)
            dump(ROOT / "report.json", report)


if __name__ == "__main__":
    main()
