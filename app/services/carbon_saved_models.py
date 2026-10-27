"""Frozen reference-regression models shared by training and map inference."""
import json
import hashlib
from functools import lru_cache
from pathlib import Path

import ee

from app.core.config import get_settings
from app.services.gee_common import AnalysisError


FEATURES = ["blue", "green", "red", "nir", "swir1", "swir2", "ndvi", "ndmi", "elevation", "slope"]


def predictors(region, year, feature_names):
    from app.services.carbon_pool_service import _landsat_predictors

    image = _landsat_predictors(region, year)
    if "s1_vv" in feature_names:
        radar = (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(region)
                 .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                 .filter(ee.Filter.eq("instrumentMode", "IW"))
                 .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
                 .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH")))
        image = image.addBands(radar.select(["VV", "VH"]).median().rename(["s1_vv", "s1_vh"]))
    return image.select(feature_names)


def tree_string(estimator, names):
    """Lossless numeric sklearn tree export to EE's R tree format."""
    tree = estimator.tree_
    lines = [f"1) root {tree.n_node_samples[0]} 0 0"]

    def visit(node, number, depth):
        feature = tree.feature[node]
        if feature < 0:
            return
        for child, sign, child_number in [(tree.children_left[node], "<=", number * 2),
                                           (tree.children_right[node], ">", number * 2 + 1)]:
            leaf = tree.children_left[child] == tree.children_right[child]
            value = float(tree.value[child].reshape(-1)[0])
            lines.append(f"{'  ' * depth}{child_number}) {names[feature]}{sign}{tree.threshold[node]:.17g} "
                         f"{tree.n_node_samples[child]} 0 {value:.17g}" + (" *" if leaf else ""))
            visit(child, child_number, depth + 1)

    if tree.n_node_samples.size == 1:
        lines[0] = f"1) root {tree.n_node_samples[0]} 0 {float(tree.value[0].reshape(-1)[0]):.17g} *"
    else:
        visit(0, 1, 1)
    return "\n".join(lines) + "\n"


def ensemble_image(image, artifact):
    # sklearn's tree predictor casts features to float32 before comparisons.
    image = image.toFloat()
    trees = artifact["trees"]
    if artifact["algorithm"] == "gradient_boosting":
        output = ee.Image.constant(artifact["initial_prediction"]).toDouble()
        for tree in trees:
            term = image.classify(ee.Classifier.decisionTree(tree).setOutputMode("REGRESSION"))
            output = output.add(term.multiply(artifact["learning_rate"]))
    else:
        output = image.classify(ee.Classifier.decisionTreeEnsemble(trees).setOutputMode("REGRESSION"))
    return output.max(0).rename("agb").updateMask(image.mask().reduce(ee.Reducer.min()))


@lru_cache(maxsize=32)
def _read_artifact(path, modified):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def saved_model_image(roi, model, dataset_meta, analysis_year=None):
    info = model["metadata_json"]
    bbox = info["training_bbox"]
    if not ee.Geometry.Rectangle(bbox, proj="EPSG:4326", geodesic=False).contains(roi.bounds(maxError=100), maxError=100).getInfo():
        raise AnalysisError("Model regional ini hanya dikalibrasi di Jawa. Gunakan raster referensi atau model lain untuk AOI di luar wilayah training.", 422)
    requested = dataset_meta.get("selection_year") or dataset_meta.get("year")
    if isinstance(requested, int) and requested != info["reference_year"]:
        raise AnalysisError(f"Model dikalibrasi terhadap referensi {info['reference_year']}. Pilih tahun tersebut atau gunakan raster referensi langsung.", 422)
    root = get_settings().model_path.resolve()
    path = (root / info["frozen_ensemble_file"]).resolve()
    if path.parent != root or not path.is_file():
        raise AnalysisError("Artefak model tersimpan belum tersedia pada backend ini.", 503)
    if hashlib.sha256(path.read_bytes()).hexdigest() != info["artifact_sha256"]:
        raise AnalysisError("Checksum artefak model tidak sesuai. Deploy ulang paket model yang lengkap.", 503)
    artifact = _read_artifact(str(path), path.stat().st_mtime_ns)
    covariate_year = int(analysis_year or info["covariate_year"])
    image = predictors(roi, covariate_year, info["feature_names"])
    return ensemble_image(image, artifact), {
        "n_samples": info["n_samples"], "validation_samples": info["validation_samples"],
        "validation_metrics": info["validation_metrics"], "validation_type": "spatial_reference_holdout",
        "validation_note": "Validasi terhadap raster referensi pada blok spasial terpisah; akurasi lapangan belum ditetapkan.",
        "covariate_year": covariate_year, "reference_covariate_year": info["covariate_year"], "training_bbox": bbox,
        "deployment_mode": "frozen_ensemble", "trained_at": info["trained_at"],
    }


def saved_reference_models(target_dataset=None, name=None, db=None):
    from contextlib import nullcontext
    from app.db.models.uploaded_model import UploadedModel
    from app.db.session import SessionLocal

    with (nullcontext(db) if db is not None else SessionLocal()) as db:
        query = db.query(UploadedModel).filter_by(model_type="carbon", is_active=True)
        if name:
            query = query.filter_by(name=name)
        models = []
        for row in query.all():
            meta = row.metadata_json or {}
            if meta.get("method") != "saved_reference_ensemble":
                continue
            if target_dataset and target_dataset not in meta.get("compatible_reference_datasets", []):
                continue
            if (get_settings().model_path / meta.get("frozen_ensemble_file", "")).is_file():
                models.append(row.to_dict_public())
        return models
