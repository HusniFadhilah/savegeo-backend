"""Versioned LULC classifiers and published predictions from foundation models.

Reference-trained RF is operational but is not a field-validated model. A
foundation-model adapter only becomes available after real prediction assets
and their class schema are configured; a backbone alone is insufficient.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock

import ee

from app.core.config import get_settings
from app.registries.landcover_dataset_registry import (
    LAND_COVER_DATASET_OPTIONS, LAND_COVER_LEGENDS, LAND_COVER_NATIVE_SCALE,
    MAPBIOMAS_41_ASSET, MAPBIOMAS_CLASS_CODES, MAPBIOMAS_DETAIL_LEGEND,
)
from app.services.gee_common import AnalysisError

# Public dataset identifier, not an authentication credential.
LCFM_DATASET = "Copernicus_LCFM10"
MODEL_KEYS = {"AlphaEarth_RF", "Sentinel1_2_RF", "TerraMind_LULC", "TESSERA_LULC", "Prithvi_LULC", "Copernicus_LCFM10"}
RF_KEYS = {"AlphaEarth_RF", "Sentinel1_2_RF"}
TRAINING_METADATA_KEYS = {"training_year", "training_source", "training_sample_count", "trained_class_codes", "missing_training_classes", "validation_kind", "independent_accuracy", "quality_note"}
ALPHA_COLLECTION = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
_LOCK = RLock()
_TRAIN_LOCK = RLock()
_CLASSIFIERS: dict[str, tuple] = {}
LCFM_LEGEND = {str(i): {"label": label, "color": color} for i, label, color in [
    (10, "Tree cover", "#006400"), (20, "Shrubland", "#ffbb22"),
    (30, "Grassland", "#ffff4c"), (40, "Cropland", "#f096ff"),
    (50, "Herbaceous wetland", "#0096a0"), (60, "Mangroves", "#00cf75"),
    (70, "Moss and lichen", "#fae6a0"), (80, "Bare / sparse vegetation", "#b4b4b4"),
    (90, "Built-up", "#fa0000"), (100, "Permanent water bodies", "#0064c8"),
    (110, "Snow and ice", "#f0f0f0"),
]}
_ASSET_PATTERN = re.compile(r"^projects/[A-Za-z0-9_.-]+/assets/[A-Za-z0-9_./{}-]+$")
_TRAINING_TILES = [
    [110.15, -7.25, 110.65, -6.75], [106.5, -6.65, 107.0, -6.15],
    [112.4, -7.6, 112.9, -7.1], [103.5, -1.9, 104.0, -1.4],
    [100.1, 1.3, 100.6, 1.8], [109.0, -0.4, 109.5, 0.1],
    [113.4, -3.0, 113.9, -2.5], [116.7, -0.8, 117.2, -0.3],
    [119.3, -5.4, 119.8, -4.9], [140.4, -2.9, 140.9, -2.4],
    [105.8, -2.4, 106.3, -1.9], [111.9, -2.8, 112.4, -2.3],
]


def model_config_path() -> Path:
    return Path(get_settings().landcover_model_config_path).resolve()


def read_model_config() -> dict:
    path = model_config_path()
    if not path.is_file():
        return {}
    try:
        return validate_model_config(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        raise AnalysisError("Konfigurasi model land cover tidak valid; periksa Admin → Model Land Cover.", 503) from exc


def validate_model_config(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - MODEL_KEYS:
        raise ValueError("Konfigurasi harus berupa object dengan ID model dari katalog SAVEGEO.")
    clean = {}
    for model_id, config in value.items():
        if not isinstance(config, dict):
            raise ValueError(f"{model_id}: konfigurasi harus berupa object.")
        config = dict(config)
        if "training_metadata" in config and (not isinstance(config["training_metadata"], dict) or set(config["training_metadata"]) - TRAINING_METADATA_KEYS):
            raise ValueError(f"{model_id}: training_metadata tidak valid.")
        if "enabled" in config and type(config["enabled"]) is not bool:
            raise ValueError(f"{model_id}: enabled harus boolean.")
        for field in ("model_version", "class_schema", "accuracy_note", "validation_kind", "coverage"):
            if field in config and (not isinstance(config[field], str) or not config[field].strip() or len(config[field]) > 1000):
                raise ValueError(f"{model_id}: {field} harus berupa teks 1–1000 karakter.")
        years = config.get("supported_years", [])
        if not isinstance(years, list) or any(type(y) is not int or not 1985 <= y <= datetime.now(UTC).year for y in years):
            raise ValueError(f"{model_id}: supported_years harus berisi tahun valid.")
        config["supported_years"] = sorted(set(years))
        for field in ("classifier_asset", "annual_asset_template"):
            asset = config.get(field)
            if asset and (not isinstance(asset, str) or not _ASSET_PATTERN.fullmatch(asset) or ".." in asset or set(re.findall(r"\{([^}]+)\}", asset)) - {"year"}):
                raise ValueError(f"{model_id}: {field} harus merupakan asset EE projects/.../assets/... dengan placeholder {{year}} opsional.")
        if config.get("annual_asset_template") and (not years or not config.get("model_version")):
            raise ValueError(f"{model_id}: aset prediksi memerlukan supported_years dan model_version.")
        if config.get("classifier_asset") and model_id not in RF_KEYS:
            raise ValueError(f"{model_id}: publikasikan raster hasil model, bukan classifier EE.")
        if config.get("classifier_asset") and not config.get("model_version"):
            raise ValueError(f"{model_id}: classifier_asset memerlukan model_version.")
        if config.get("classifier_asset") and ("{" in config["classifier_asset"] or config.get("annual_asset_template")):
            raise ValueError(f"{model_id}: pilih classifier_asset tanpa placeholder ATAU annual_asset_template.")
        for field, low, high, default in (("trees", 10, 300, 80), ("points_per_class", 5, 200, 40), ("training_year", 2017, 2024, 2024), ("native_scale", 10, 1000, 30 if model_id == "Prithvi_LULC" else 10)):
            n = config.get(field, default)
            if type(n) is not int or not low <= n <= high:
                raise ValueError(f"{model_id}: {field} harus {low}–{high}.")
            config[field] = n
        classes = config.get("classes", LCFM_LEGEND if model_id == "Copernicus_LCFM10" else MAPBIOMAS_DETAIL_LEGEND)
        if not isinstance(classes, dict) or not 2 <= len(classes) <= 100:
            raise ValueError(f"{model_id}: classes harus memuat 2–100 kelas.")
        for code, entry in classes.items():
            if not str(code).isdigit() or not 0 <= int(code) < 1000 or not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not entry["label"].strip() or not re.fullmatch(r"#[0-9a-fA-F]{6}", str(entry.get("color", ""))):
                raise ValueError(f"{model_id}: kelas memerlukan ID 0–999, label, dan warna #RRGGBB.")
        if model_id in RF_KEYS and not config.get("classifier_asset") and not config.get("annual_asset_template") and classes != MAPBIOMAS_DETAIL_LEGEND:
            raise ValueError(f"{model_id}: schema khusus memerlukan classifier_asset yang sudah dilatih.")
        config["classes"] = {str(int(code)): {"label": entry["label"], "color": entry["color"]} for code, entry in classes.items()}
        if len(config["classes"]) != len(classes) or len({entry["label"] for entry in config["classes"].values()}) != len(classes):
            raise ValueError(f"{model_id}: ID dan label kelas harus unik.")
        config["class_schema"] = config.get("class_schema") or ("MAPBIOMAS_INDONESIA_41_13" if config["classes"] == MAPBIOMAS_DETAIL_LEGEND else "classes-" + _version(config["classes"]))
        if config.get("class_encoding", "indices") not in {"indices", "raw"}:
            raise ValueError(f"{model_id}: class_encoding harus indices atau raw.")
        for field in ("class_band", "confidence_band"):
            band = config.get(field)
            if band and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", str(band)):
                raise ValueError(f"{model_id}: nama band tidak valid.")
        clean[model_id] = config
    return clean


def write_model_config(value: dict) -> dict:
    clean = validate_model_config(value)
    path = model_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="landcover-models-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    with _LOCK:
        _CLASSIFIERS.clear()
    refresh_model_catalog()
    return clean


def refresh_model_catalog() -> dict:
    configs = read_model_config()
    with _LOCK:
        for model_id in MODEL_KEYS:
            config = configs.get(model_id, {})
            ready = config.get("enabled", True) and (model_id in RF_KEYS or bool(config.get("annual_asset_template")))
            years = config.get("supported_years") or (list(range(2017, 2026)) if model_id == "AlphaEarth_RF" else list(range(2017, datetime.now(UTC).year + 1)) if model_id == "Sentinel1_2_RF" else [])
            LAND_COVER_LEGENDS[model_id] = config.get("classes", LCFM_LEGEND if model_id == "Copernicus_LCFM10" else MAPBIOMAS_DETAIL_LEGEND)
            LAND_COVER_NATIVE_SCALE[model_id] = config.get("native_scale", 30 if model_id == "Prithvi_LULC" else 10)
            LAND_COVER_DATASET_OPTIONS[model_id].update({
                "available": bool(ready), "model_status": "configured" if ready and config else "reference_trained" if ready else "needs_configuration",
                "availability_reason": None if ready else "Aset prediksi model terlatih belum dikonfigurasi atau model dinonaktifkan.",
                "supported_years": years, "year_min": min(years) if years else None, "year_max": max(years) if years else None,
                "collection_version": config.get("model_version", "mapbiomas41-reference-rf-v2" if model_id in RF_KEYS else None),
                "resolution": f"{LAND_COVER_NATIVE_SCALE[model_id]}m", "class_count": len(LAND_COVER_LEGENDS[model_id]),
                "class_schema": config.get("class_schema", "LCFM10_11" if model_id == "Copernicus_LCFM10" else "MAPBIOMAS_INDONESIA_41_13"),
                "accuracy": config.get("accuracy_note", "Validasi lokal independen belum tersedia."),
                "monthly": model_id == "Sentinel1_2_RF" and not config.get("annual_asset_template"),
                "coverage": config.get("coverage", "Indonesia" if model_id in RF_KEYS else "Model coverage"),
                "supports_transition": len(years) > 1,
            })
    return configs


def _version(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def training_region():
    return ee.Geometry.MultiPolygon([
        [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]
        for x0, y0, x1, y1 in _TRAINING_TILES
    ], proj="EPSG:4326", geodesic=False)


def feature_image(model_id: str, year: int, aoi, start: str | None = None, end: str | None = None):
    start, end = start or f"{year}-01-01", end or f"{year + 1}-01-01"
    if model_id == "AlphaEarth_RF":
        collection = ee.ImageCollection(ALPHA_COLLECTION).filterDate(f"{year}-01-01", f"{year + 1}-01-01").filterBounds(aoi)
        if not collection.limit(1).size().getInfo():
            raise AnalysisError(f"Embedding AlphaEarth tahun {year} tidak tersedia untuk AOI.", 404)
        return collection.mosaic().select([f"A{i:02d}" for i in range(64)]).clip(aoi)

    def mask_cloud(image):
        scl = image.select("SCL")
        valid = scl.neq(0).And(scl.neq(1)).And(scl.neq(3)).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))
        return image.select(["B2", "B3", "B4", "B8", "B11", "B12"]).multiply(0.0001).updateMask(valid)

    optical = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(aoi).filterDate(start, end).filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
    radar = ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(aoi).filterDate(start, end).filter(ee.Filter.eq("instrumentMode", "IW")).filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV")).filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
    sizes = ee.Dictionary({"s2": optical.limit(1).size(), "s1": radar.limit(1).size()}).getInfo() or {}
    if not sizes.get("s2") or not sizes.get("s1"):
        raise AnalysisError("Citra Sentinel-1/2 yang diperlukan model tidak tersedia pada periode/AOI.", 404)
    image = optical.map(mask_cloud).median()
    s1 = radar.select(["VV", "VH"]).median()
    return image.addBands([
        image.normalizedDifference(["B8", "B4"]).rename("NDVI"),
        image.normalizedDifference(["B3", "B11"]).rename("MNDWI"),
        image.normalizedDifference(["B11", "B8"]).rename("NDBI"), s1,
        s1.select("VV").subtract(s1.select("VH")).rename("VV_VH"),
    ]).clip(aoi)


def build_reference_classifier(model_id: str, config: dict, region=None):
    """Sample reference labels once; keep the training table bounded and fixed."""
    year = config.get("training_year", 2024)
    cache_path = model_config_path().parent / "landcover-training" / f"reference-{_version({'key': model_id, 'year': year, 'points': config.get('points_per_class', 40), 'recipe': 'mapbiomas41-points-first-v2'})}.json"
    use_cache = region is None
    if use_cache and cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        classifier = ee.Classifier.smileRandomForest(numberOfTrees=config.get("trees", 80), seed=41).train(
            features=ee.FeatureCollection([ee.Feature(None, properties) for properties in cached["properties"]]),
            classProperty="label", inputProperties=cached["bands"],
        )
        return classifier, cached["metadata"]
    region = region or training_region()
    features = feature_image(model_id, year, region)
    labels = ee.Image(MAPBIOMAS_41_ASSET).select(f"classification_{year}")
    codes = MAPBIOMAS_CLASS_CODES
    indices = list(range(len(codes)))
    label = labels.remap(codes, indices).rename("label").toInt16()
    # Remove boundary pixels from the weak labels; their native resolution is 30m.
    homogeneous = labels.focalMin(radius=1, units="pixels").eq(labels.focalMax(radius=1, units="pixels"))
    label = label.updateMask(homogeneous)
    points = label.stratifiedSample(
        numPoints=config.get("points_per_class", 40), classBand="label", region=region,
        scale=30, projection=labels.projection(), seed=41, geometries=True, tileScale=8,
    )
    # Sample labels first. Compositing S1/S2 over every candidate reference
    # pixel just to keep 520 samples is prohibitively expensive.
    samples = features.sampleRegions(collection=points, properties=["label"], scale=30,
                                     projection=labels.projection(), geometries=False, tileScale=8).getInfo()
    rows = samples.get("features", [])
    observed = sorted({int(row["properties"]["label"]) for row in rows})
    if len(observed) < 2:
        raise AnalysisError("Sampel referensi tidak cukup untuk melatih classifier; pilih wilayah training lain.", 422)
    classifier = ee.Classifier.smileRandomForest(numberOfTrees=config.get("trees", 80), seed=41).train(
        features=ee.FeatureCollection([ee.Feature(None, row["properties"]) for row in rows]), classProperty="label", inputProperties=features.bandNames(),
    )
    metadata = {
        "training_year": year, "training_source": "MapBiomas Indonesia 4.1",
        "training_sample_count": len(rows), "trained_class_codes": [codes[i] for i in observed],
        "missing_training_classes": [code for i, code in enumerate(codes) if i not in observed],
        "validation_kind": "reference_labels", "independent_accuracy": None,
        "quality_note": "Classifier dilatih dari label referensi 30 m; akurasi independen 10 m belum tersedia.",
    }
    if use_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="reference-", suffix=".tmp", dir=cache_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"properties": [row["properties"] for row in rows], "bands": features.bandNames().getInfo(), "metadata": metadata}, stream)
            os.replace(temporary, cache_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return classifier, metadata


def get_model_image(model_id: str, year: int, aoi, start: str, end: str, threshold: float | None = None, *, with_confidence: bool = False):
    configs = refresh_model_catalog()
    config = configs.get(model_id, {})
    meta = LAND_COVER_DATASET_OPTIONS[model_id]
    if not meta["available"]:
        raise AnalysisError(meta["availability_reason"], 409, extra={"dataset": model_id, "model_status": "needs_configuration"})
    if year not in meta["supported_years"]:
        raise AnalysisError(f"{meta['name']}: tahun {year} belum tersedia.", 422)
    if meta["monthly"]:
        tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
        end = min(end, tomorrow)
        start = max(start, "2017-03-28")
        if start >= end:
            raise AnalysisError("Periode Sentinel-1/2 belum tersedia atau berada di masa depan.", 422)
    version = config.get("model_version") or _version({"key": model_id, "recipe": meta["collection_version"], **config})
    metadata = {
        "dataset": model_id, "dataset_name": meta["name"], "year": year, "requested_year": year,
        "provider_type": meta["provider_type"], "classification_backend": model_id,
        "collection_version": meta["collection_version"], "model_version": version,
        "class_schema": meta["class_schema"], "coverage": meta.get("coverage"),
        "date_range": {"start": start if meta["monthly"] else f"{year}-01-01", "end": (datetime.fromisoformat(end).date() - timedelta(days=1)).isoformat() if meta["monthly"] else f"{year}-12-31"},
    }
    confidence = None
    if config.get("annual_asset_template"):
        asset = config["annual_asset_template"].format(year=year)
        prediction = ee.Image(asset)
        image = prediction.select(config.get("class_band", "landcover")).rename("landcover").toInt16()
        metadata.update({"source_uri": asset, "quality_note": config.get("accuracy_note", "Validasi lokal independen belum tersedia."), "validation_kind": config.get("validation_kind", "not_provided")})
        if config.get("confidence_band"):
            confidence = prediction.select(config["confidence_band"])
            if threshold is not None:
                image = image.updateMask(confidence.gte(threshold))
            metadata["confidence_available"] = True
        elif threshold is not None:
            raise AnalysisError("Model ini tidak menyediakan band confidence untuk filter probabilitas.", 422)
    else:
        features = feature_image(model_id, year, aoi, start, end)
        if config.get("classifier_asset"):
            classifier = ee.Classifier.load(config["classifier_asset"])
            metadata.update({"source_uri": config["classifier_asset"], "validation_kind": config.get("validation_kind", "not_provided"), "quality_note": config.get("accuracy_note", "Validasi lokal independen belum tersedia.")})
            metadata.update(config.get("training_metadata", {}))
        else:
            cache_key = _version({"key": model_id, **config})
            with _TRAIN_LOCK:
                if cache_key not in _CLASSIFIERS:
                    _CLASSIFIERS[cache_key] = build_reference_classifier(model_id, config)
                classifier, training_meta = _CLASSIFIERS[cache_key]
            metadata.update(training_meta)
        image = features.classify(classifier).rename("landcover")
        if config.get("class_encoding", "indices") == "indices":
            codes = sorted(int(code) for code in LAND_COVER_LEGENDS[model_id])
            image = image.remap(list(range(len(codes))), codes).rename("landcover")
        # Limit reference-trained models to the label source's actual footprint.
        if not config.get("classifier_asset"):
            image = image.updateMask(ee.Image(MAPBIOMAS_41_ASSET).select("classification_2024").mask())
        if threshold is not None or with_confidence:
            votes = features.classify(classifier.setOutputMode("MULTIPROBABILITY"))
            confidence = votes.arrayReduce(ee.Reducer.max(), [0]).arrayGet([0])
            if threshold is not None:
                image = image.updateMask(confidence.gte(threshold))
        metadata.update({"confidence_available": True, "confidence_calibrated": False})
    codes = [int(code) for code in LAND_COVER_LEGENDS[model_id]]
    image = image.updateMask(image.remap(codes, [1] * len(codes), 0)).clip(aoi)
    if with_confidence:
        return image, metadata, confidence.rename("confidence").updateMask(image.mask()).clip(aoi) if confidence is not None else None
    return image, metadata
