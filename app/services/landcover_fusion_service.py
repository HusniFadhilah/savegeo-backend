"""Quality-gated optical/SAR fusion for land-cover classification and change.

For every pixel p and period t the service derives two input-quality scores:

    Q_o = V_o * (1 - C_o) * A_o      (Sentinel-2 SR, SCL mask)
    Q_s = V_s * S_s * A_s            (Sentinel-1 GRD IW, VV/VH)

V = at least one clear/valid observation, C_o = median s2cloudless cloud
probability of the clear (SCL-unmasked) Sentinel-2 observations, i.e. the
residual thin-cloud/haze risk left in the composite, A = number of valid
observations relative to a reference count (capped at 1), and S_s =
1 - local coefficient of variation of the speckle-filtered VV backscatter
(residual-speckle index, clamped to [0, 1]). The scores describe input
fitness for inference; they are not classification accuracy.

Three Random Forest classifiers are trained once on the same MapBiomas 4.1
reference samples: joint (optical + SAR + terrain), optical (optical +
terrain) and SAR (SAR + terrain). Per pixel, ``routing`` mode picks:

    3 joint   if Q_o >= q_min and Q_s >= q_min
    1 optical if only Q_o >= q_min
    2 SAR     if only Q_s >= q_min
    0 no-data otherwise

``weighted`` mode instead blends the class probabilities at decision level,
P = (Q_o * P_opt + Q_s * P_sar) / max(Q_o + Q_s, eps), for pixels where at
least one score reaches q_min. Optical bands and SAR backscatter are not
averaged with each other: they live in different feature spaces.

Change between t0 and t1 uses the class-probability vectors:

    D(p) = 1/2 * sum_k |P_t0,k(p) - P_t1,k(p)|

(total-variation distance: 0 for identical class-probability vectors, 1 for
vectors with disjoint support; unlike 1 - sum_k P_t0,k * P_t1,k it does not
report change merely because both vectors are uncertain)

and is valid only when D >= tau_change, the top classes differ and both
periods pass the quality gate. Areas are summed on the EPSG:6933 equal-area
grid used by the other land-cover change endpoints.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, timedelta
from threading import RLock

import ee

from app.core.config import get_settings
from app.registries.landcover_dataset_registry import (
    MAPBIOMAS_41_ASSET,
    MAPBIOMAS_CLASS_CODES,
    MAPBIOMAS_DETAIL_LEGEND,
)
from app.services.gee_common import AnalysisError, create_geometry_from_payload, get_tile_url
from app.services.landcover_model_service import _version, model_config_path, training_region
from app.services.landcover_service import LC_EQUAL_AREA_CRS

logger = logging.getLogger(__name__)

METHOD_VERSION = "quality-gated-fusion-v2"
OPTICAL_BANDS = ["B2", "B3", "B4", "B8", "B11", "B12"]
OPTICAL_FEATURES = OPTICAL_BANDS + ["NDVI", "MNDWI", "NDBI"]
SAR_FEATURES = ["VV", "VH", "VV_VH"]
AUX_FEATURES = ["elevation", "slope"]
PATH_FEATURES = {
    "joint": OPTICAL_FEATURES + SAR_FEATURES + AUX_FEATURES,
    "optical": OPTICAL_FEATURES + AUX_FEATURES,
    "sar": SAR_FEATURES + AUX_FEATURES,
}
ROUTES = {0: "no_data", 1: "optical_dominant", 2: "sar_dominant", 3: "joint_fusion"}
ROUTE_LEGEND = [
    {"value": 1, "label": "Optical-dominant", "color": "#f59e0b"},
    {"value": 2, "label": "SAR-dominant", "color": "#2563eb"},
    {"value": 3, "label": "Joint-fusion", "color": "#059669"},
]
FUSION_MODES = {"routing", "weighted"}
# SCL: 0 no data, 1 saturated/defective, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow.
_SCL_INVALID = (1, 3, 8, 9, 10, 11)
_DEM = "USGS/SRTMGL1_003"
_EPS = 1e-6
_TRAIN_LOCK = RLock()
_CLASSIFIERS: dict[str, tuple[dict, dict]] = {}
_LC_PALETTE = [entry["color"] for entry in MAPBIOMAS_DETAIL_LEGEND.values()]
_QUALITY_PALETTE = ["#7f1d1d", "#f59e0b", "#fde68a", "#86efac", "#166534"]


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

def _iso(value: str, name: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise AnalysisError(f"Tanggal {name} tidak valid", 400) from exc


def _period(data: dict, prefix: str, year_key: str) -> tuple[str, str]:
    """Return (start, exclusive_end) for one period."""
    start, end = data.get(f"{prefix}_start_date"), data.get(f"{prefix}_end_date")
    if start and end:
        start_d, end_d = _iso(start, f"{prefix}_start_date"), _iso(end, f"{prefix}_end_date")
    else:
        if data.get(year_key) is None:
            raise AnalysisError(f"{year_key} atau {prefix}_start_date/{prefix}_end_date wajib diisi", 400)
        year = int(data[year_key])
        start_month, end_month = int(data.get("start_month", 1)), int(data.get("end_month", 12))
        if not (1 <= start_month <= end_month <= 12):
            raise AnalysisError("Rentang bulan tidak valid", 400)
        start_d = date(year, start_month, 1)
        end_d = date(year, 12, 31) if end_month == 12 else date(year, end_month + 1, 1) - timedelta(days=1)
    if start_d < date(2017, 3, 28):
        start_d = date(2017, 3, 28)  # Sentinel-2 SR harmonized + S1 IW dual-pol availability
    if end_d > date.today():
        end_d = date.today()
    if start_d > end_d:
        raise AnalysisError(f"Periode {prefix} kosong atau berada di masa depan", 422)
    return start_d.isoformat(), (end_d + timedelta(days=1)).isoformat()


def parse_params(data: dict) -> dict:
    if not data or not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    t0 = _period(data, "from", "from_year")
    t1 = _period(data, "to", "to_year")
    if t0[0] >= t1[0]:
        raise AnalysisError("Periode dasar harus lebih awal dari periode pembanding", 400)
    mode = str(data.get("fusion_mode", "routing"))
    if mode not in FUSION_MODES:
        raise AnalysisError("fusion_mode harus 'routing' atau 'weighted'", 400)

    def unit(key: str, default: float) -> float:
        value = float(data.get(key, default))
        if not 0.0 <= value <= 1.0:
            raise AnalysisError(f"{key} harus berada pada rentang 0-1", 400)
        return value

    confidence = data.get("confidence_threshold")
    return {
        "t0": t0, "t1": t1, "fusion_mode": mode,
        "q_min": unit("q_min", 0.5),
        "tau_change": unit("tau_change", 0.2),
        "confidence_threshold": None if confidence in (None, "") else unit("confidence_threshold", 0),
        "optical_reference_count": max(1, min(int(data.get("optical_reference_count", 3)), 30)),
        "sar_reference_count": max(1, min(int(data.get("sar_reference_count", 2)), 30)),
        "scale": max(10, min(int(data.get("scale", 20)), 100)),
    }


# --------------------------------------------------------------------------
# Per-period inputs and quality
# --------------------------------------------------------------------------

def _empty(bands: list[str]):
    return ee.Image.constant([0] * len(bands)).rename(bands).toFloat().updateMask(ee.Image(0))


def period_inputs(aoi, start: str, end: str, *, optical_reference_count: int = 3, sar_reference_count: int = 2) -> dict:
    """Feature stack plus Q_o/Q_s for one period (server-side ee objects)."""
    s2 = ee.ImageCollection(ee.Join.saveFirst("cloud_probability").apply(
        primary=ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(aoi).filterDate(start, end),
        secondary=ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY").filterBounds(aoi).filterDate(start, end),
        condition=ee.Filter.equals(leftField="system:index", rightField="system:index"),
    ))
    s1 = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi).filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .select(["VV", "VH"])
    )
    counts = ee.Dictionary({"s2": s2.size(), "s1": s1.size()}).getInfo() or {}

    if counts.get("s2"):
        def prepare(image):
            scl = image.select("SCL")
            observed = scl.neq(0)
            clear = observed
            for code in _SCL_INVALID:
                clear = clear.And(scl.neq(code))
            reflectance = image.select(OPTICAL_BANDS).multiply(0.0001).updateMask(clear)
            cloud = ee.Image(image.get("cloud_probability")).select("probability").divide(100).updateMask(clear)
            return (
                reflectance.addBands(cloud.rename("cloud").toFloat())
                .addBands(observed.rename("observed").toFloat())
                .addBands(clear.rename("clear").toFloat())
            )

        prepared = s2.map(prepare)
        clear_count = prepared.select("clear").sum().unmask(0)
        optical = prepared.select(OPTICAL_BANDS).median()
        optical = optical.addBands([
            optical.normalizedDifference(["B8", "B4"]).rename("NDVI"),
            optical.normalizedDifference(["B3", "B11"]).rename("MNDWI"),
            optical.normalizedDifference(["B11", "B8"]).rename("NDBI"),
        ])
        v_o = clear_count.gt(0)
        c_o = prepared.select("cloud").median().unmask(1)
        a_o = clear_count.divide(optical_reference_count).min(1)
        q_o = v_o.multiply(ee.Image(1).subtract(c_o)).multiply(a_o)
    else:
        optical, clear_count = _empty(OPTICAL_FEATURES), ee.Image(0)
        q_o = ee.Image(0)

    if counts.get("s1"):
        sar_count = s1.select("VV").count().unmask(0)
        backscatter = s1.median().focal_median(30, "circle", "meters")
        sar = backscatter.addBands(backscatter.select("VV").subtract(backscatter.select("VH")).rename("VV_VH"))
        linear = ee.Image(10).pow(backscatter.select("VV").divide(10))
        local = linear.reduceNeighborhood(
            reducer=ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
            kernel=ee.Kernel.square(30, "meters"),
        )
        cv = local.select(1).divide(local.select(0).max(_EPS))
        s_s = ee.Image(1).subtract(cv).clamp(0, 1).unmask(0)
        v_s = sar_count.gt(0)
        a_s = sar_count.divide(sar_reference_count).min(1)
        q_s = v_s.multiply(s_s).multiply(a_s)
    else:
        sar, sar_count = _empty(SAR_FEATURES), ee.Image(0)
        q_s = ee.Image(0)

    dem = ee.Image(_DEM).select("elevation").toFloat()
    aux = dem.addBands(ee.Terrain.slope(dem).rename("slope"))
    return {
        "features": optical.select(OPTICAL_FEATURES).addBands(sar.select(SAR_FEATURES)).addBands(aux).toFloat(),
        "q_o": q_o.clamp(0, 1).rename("Q_o").toFloat(),
        "q_s": q_s.clamp(0, 1).rename("Q_s").toFloat(),
        "optical_valid_count": clear_count.rename("optical_valid_count"),
        "sar_valid_count": sar_count.rename("sar_valid_count"),
        "scene_counts": {"sentinel2": int(counts.get("s2") or 0), "sentinel1": int(counts.get("s1") or 0)},
    }


# --------------------------------------------------------------------------
# Classifiers (one sample table, three feature subsets)
# --------------------------------------------------------------------------

def _reference_samples(training_year: int, points_per_class: int) -> dict:
    recipe = {"recipe": METHOD_VERSION, "year": training_year, "points": points_per_class, "features": PATH_FEATURES["joint"]}
    cache_path = model_config_path().parent / "landcover-training" / f"fusion-reference-{_version(recipe)}.json"
    if cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    region = training_region()
    stack = period_inputs(region, f"{training_year}-01-01", f"{training_year + 1}-01-01")["features"]
    labels = ee.Image(MAPBIOMAS_41_ASSET).select(f"classification_{training_year}")
    label = labels.remap(MAPBIOMAS_CLASS_CODES, list(range(len(MAPBIOMAS_CLASS_CODES)))).rename("label").toInt16()
    # Boundary pixels of the 30 m reference are ambiguous; keep homogeneous 3x3 neighbourhoods only.
    homogeneous = labels.focalMin(radius=1, units="pixels").eq(labels.focalMax(radius=1, units="pixels"))
    points = label.updateMask(homogeneous).stratifiedSample(
        numPoints=points_per_class, classBand="label", region=region,
        scale=30, projection=labels.projection(), seed=41, geometries=True, tileScale=8,
    )
    rows = stack.sampleRegions(
        collection=points, properties=["label"], scale=30,
        projection=labels.projection(), geometries=False, tileScale=8,
    ).getInfo().get("features", [])
    properties = [row["properties"] for row in rows if all(row["properties"].get(b) is not None for b in PATH_FEATURES["joint"])]
    observed = sorted({int(p["label"]) for p in properties})
    if len(observed) < 2:
        raise AnalysisError("Sampel referensi fusi tidak cukup untuk melatih classifier", 422)
    payload = {
        "properties": properties,
        "metadata": {
            "training_year": training_year, "training_source": "MapBiomas Indonesia 4.1",
            "training_sample_count": len(properties),
            "trained_class_codes": [MAPBIOMAS_CLASS_CODES[i] for i in observed],
            "missing_training_classes": [code for i, code in enumerate(MAPBIOMAS_CLASS_CODES) if i not in observed],
            "validation_kind": "reference_labels", "independent_accuracy": None,
            "quality_note": "Tiga classifier (gabungan, optik, SAR) dilatih dari sampel referensi 30 m yang sama; akurasi independen belum tersedia.",
        },
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="fusion-", suffix=".tmp", dir=cache_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        os.replace(temporary, cache_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def get_classifiers(training_year: int = 2024, points_per_class: int = 40, trees: int = 80) -> tuple[dict, dict]:
    key = f"{training_year}:{points_per_class}:{trees}"
    with _TRAIN_LOCK:
        if key not in _CLASSIFIERS:
            reference = _reference_samples(training_year, points_per_class)
            table = ee.FeatureCollection([ee.Feature(None, p) for p in reference["properties"]])
            classifiers = {
                path: ee.Classifier.smileRandomForest(numberOfTrees=trees, seed=41)
                .train(features=table, classProperty="label", inputProperties=bands)
                .setOutputMode("MULTIPROBABILITY")
                for path, bands in PATH_FEATURES.items()
            }
            metadata = dict(reference["metadata"])
            metadata["class_index_codes"] = metadata["trained_class_codes"]
            _CLASSIFIERS[key] = (classifiers, metadata)
        return _CLASSIFIERS[key]


# --------------------------------------------------------------------------
# Fusion
# --------------------------------------------------------------------------

def route_image(q_o, q_s, q_min: float):
    optical_ok, sar_ok = q_o.gte(q_min), q_s.gte(q_min)
    return (
        ee.Image(0)
        .where(optical_ok.And(sar_ok.Not()), 1)
        .where(sar_ok.And(optical_ok.Not()), 2)
        .where(optical_ok.And(sar_ok), 3)
        .rename("route").toInt8()
    )


def fuse_period(inputs: dict, classifiers: dict, class_codes: list[int], params: dict, aoi) -> dict:
    names = [f"p{i}" for i in range(len(class_codes))]

    def probabilities(path: str):
        return inputs["features"].select(PATH_FEATURES[path]).classify(classifiers[path]).arrayFlatten([names]).unmask(0)

    q_o, q_s = inputs["q_o"], inputs["q_s"]
    route = route_image(q_o, q_s, params["q_min"])
    if params["fusion_mode"] == "routing":
        prob = (
            probabilities("joint")
            .where(route.eq(1), probabilities("optical"))
            .where(route.eq(2), probabilities("sar"))
        )
    else:
        prob = (
            probabilities("optical").multiply(q_o)
            .add(probabilities("sar").multiply(q_s))
            .divide(q_o.add(q_s).max(_EPS))
        )
    footprint = ee.Image(MAPBIOMAS_41_ASSET).select("classification_2024").mask().gt(0)
    valid = route.gt(0).And(footprint)
    confidence = prob.reduce(ee.Reducer.max()).rename("confidence")
    if params["confidence_threshold"] is not None:
        valid = valid.And(confidence.gte(params["confidence_threshold"]))
    index = prob.toArray().arrayArgmax().arrayGet([0])
    label = index.remap(list(range(len(class_codes))), class_codes).rename("landcover").toInt16()
    return {
        "route": route.updateMask(footprint).clip(aoi),
        "probabilities": prob.updateMask(valid).clip(aoi),
        "label": label.updateMask(valid).clip(aoi),
        "confidence": confidence.updateMask(valid).clip(aoi),
        "valid": valid.unmask(0).clip(aoi),
    }


def change_score(prob_t0, prob_t1):
    return prob_t0.subtract(prob_t1).abs().reduce(ee.Reducer.sum()).multiply(0.5).clamp(0, 1).rename("D")


# --------------------------------------------------------------------------
# Statistics and tiles
# --------------------------------------------------------------------------

def _grouped_area(image, band_name: str, aoi, scale: int):
    return ee.Image.pixelArea().divide(10000).rename("area").addBands(image.rename(band_name)).reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName=band_name),
        geometry=aoi, scale=scale, crs=LC_EQUAL_AREA_CRS,
        maxPixels=int(get_settings().max_pixels), bestEffort=False, tileScale=4,
    )


def _mean(image, aoi, scale: int):
    return image.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=aoi, scale=scale, crs=LC_EQUAL_AREA_CRS,
        maxPixels=int(get_settings().max_pixels), bestEffort=False, tileScale=4,
    )


def _route_summary(groups: list[dict]) -> dict:
    areas = {name: 0.0 for name in ROUTES.values()}
    for group in groups or []:
        areas[ROUTES.get(int(group["route"]), "no_data")] += float(group.get("sum") or 0)
    total = sum(areas.values())
    return {
        "area_ha": {k: round(v, 2) for k, v in areas.items()},
        "percentage": {k: round(v / total * 100, 2) if total else 0.0 for k, v in areas.items()},
    }


def _tile(image, vis: dict, name: str) -> str | None:
    tile = get_tile_url(image, vis, name)
    return tile["tile_url"] if tile else None


def _lc_vis_image(label):
    return label.remap(MAPBIOMAS_CLASS_CODES, list(range(len(MAPBIOMAS_CLASS_CODES))))


def analyze_landcover_fusion(data: dict) -> dict:
    params = parse_params(data)
    aoi = create_geometry_from_payload(data["aoi"])
    classifiers, training = get_classifiers()
    class_codes = training["class_index_codes"]
    scale = params["scale"]

    periods = {}
    for key in ("t0", "t1"):
        start, end = params[key]
        inputs = period_inputs(
            aoi, start, end,
            optical_reference_count=params["optical_reference_count"],
            sar_reference_count=params["sar_reference_count"],
        )
        periods[key] = {"inputs": inputs, "fused": fuse_period(inputs, classifiers, class_codes, params, aoi)}

    f0, f1 = periods["t0"]["fused"], periods["t1"]["fused"]
    both_valid = f0["valid"].And(f1["valid"])
    d_score = change_score(f0["probabilities"], f1["probabilities"]).updateMask(both_valid).clip(aoi)
    labels_differ = f0["label"].neq(f1["label"])
    changed = d_score.gte(params["tau_change"]).And(labels_differ).unmask(0).And(both_valid)
    suppressed = labels_differ.And(d_score.lt(params["tau_change"])).unmask(0).And(both_valid)
    transition = (
        f0["label"].multiply(1000)
        .add(f1["label"].where(changed.Not(), f0["label"]))
        .updateMask(both_valid).rename("transition")
    )

    area_band = ee.Image.pixelArea().divide(10000)
    stats = ee.Dictionary({
        "aoi_area_ha": aoi.area(1).divide(10000),
        "routes_t0": _grouped_area(f0["route"].unmask(0), "route", aoi, scale).get("groups"),
        "routes_t1": _grouped_area(f1["route"].unmask(0), "route", aoi, scale).get("groups"),
        "quality_t0": _mean(periods["t0"]["inputs"]["q_o"].addBands(periods["t0"]["inputs"]["q_s"]), aoi, scale),
        "quality_t1": _mean(periods["t1"]["inputs"]["q_o"].addBands(periods["t1"]["inputs"]["q_s"]), aoi, scale),
        "transitions": _grouped_area(transition, "transition", aoi, scale).get("groups"),
        "areas": area_band.updateMask(both_valid).rename("valid")
            .addBands(area_band.updateMask(changed).rename("changed"))
            .addBands(area_band.updateMask(suppressed).rename("suppressed"))
            .reduceRegion(reducer=ee.Reducer.sum(), geometry=aoi, scale=scale, crs=LC_EQUAL_AREA_CRS,
                          maxPixels=int(get_settings().max_pixels), bestEffort=False, tileScale=4),
        "d_mean": _mean(d_score, aoi, scale).get("D"),
        "confidence_t0": _mean(f0["confidence"], aoi, scale).get("confidence"),
        "confidence_t1": _mean(f1["confidence"], aoi, scale).get("confidence"),
    }).getInfo()

    legend = {str(code): MAPBIOMAS_DETAIL_LEGEND[str(code)] for code in MAPBIOMAS_CLASS_CODES}
    flows, gains, losses = [], {}, {}
    for group in stats.get("transitions") or []:
        code = int(float(group["transition"]))
        before, after = divmod(code, 1000)
        area = float(group.get("sum") or 0)
        if str(before) not in legend or str(after) not in legend:
            continue
        from_label, to_label = legend[str(before)]["label"], legend[str(after)]["label"]
        if before != after:
            losses[from_label] = losses.get(from_label, 0.0) + area
            gains[to_label] = gains.get(to_label, 0.0) + area
        flows.append({
            "from": from_label, "to": to_label, "from_class": before, "to_class": after,
            "area": round(area, 2), "changed": before != after,
            "from_color": legend[str(before)]["color"], "to_color": legend[str(after)]["color"],
        })
    flows.sort(key=lambda item: item["area"], reverse=True)
    areas = stats.get("areas") or {}
    valid_ha = float(areas.get("valid") or 0)
    if valid_ha <= 0:
        raise AnalysisError("Tidak ada piksel yang lolos ambang kualitas pada kedua periode; turunkan q_min atau perpanjang periode.", 422)
    changed_ha = float(areas.get("changed") or 0)
    aoi_ha = float(stats.get("aoi_area_ha") or 0)
    labels_all = {**{class_label: 0 for class_label in gains}, **{class_label: 0 for class_label in losses}}
    net_changes = sorted(
        ({"class": label, "gain": round(gains.get(label, 0), 2), "loss": round(losses.get(label, 0), 2),
          "net": round(gains.get(label, 0) - losses.get(label, 0), 2)} for label in labels_all),
        key=lambda item: abs(item["net"]), reverse=True,
    )

    lc_vis = {"min": 0, "max": len(MAPBIOMAS_CLASS_CODES) - 1, "palette": _LC_PALETTE}
    route_vis = {"min": 1, "max": 3, "palette": [item["color"] for item in ROUTE_LEGEND]}
    quality_vis = {"min": 0, "max": 1, "palette": _QUALITY_PALETTE}
    tiles = {
        "route_t0": _tile(f0["route"].selfMask(), route_vis, "Jalur fusi t0"),
        "route_t1": _tile(f1["route"].selfMask(), route_vis, "Jalur fusi t1"),
        "q_o_t0": _tile(periods["t0"]["inputs"]["q_o"].clip(aoi), quality_vis, "Q_o t0"),
        "q_s_t0": _tile(periods["t0"]["inputs"]["q_s"].clip(aoi), quality_vis, "Q_s t0"),
        "q_o_t1": _tile(periods["t1"]["inputs"]["q_o"].clip(aoi), quality_vis, "Q_o t1"),
        "q_s_t1": _tile(periods["t1"]["inputs"]["q_s"].clip(aoi), quality_vis, "Q_s t1"),
        "landcover_t0": _tile(_lc_vis_image(f0["label"]), lc_vis, "Tutupan lahan fusi t0"),
        "landcover_t1": _tile(_lc_vis_image(f1["label"]), lc_vis, "Tutupan lahan fusi t1"),
        "change_score": _tile(d_score, {"min": 0, "max": 1, "palette": ["#f8fafc", "#fde047", "#dc2626"]}, "Skor perubahan D"),
        "changed": _tile(_lc_vis_image(f1["label"]).updateMask(changed.selfMask()), lc_vis, "Perubahan valid (kelas tujuan)"),
    }
    if not all(tiles.values()):
        raise AnalysisError("Tile hasil fusi gagal dibuat", 502)

    def period_report(key: str) -> dict:
        quality = stats.get(f"quality_{key}") or {}
        start, end = params[key]
        return {
            "start_date": start,
            "end_date": (date.fromisoformat(end) - timedelta(days=1)).isoformat(),
            "scene_counts": periods[key]["inputs"]["scene_counts"],
            "mean_q_o": round(float(quality.get("Q_o") or 0), 4),
            "mean_q_s": round(float(quality.get("Q_s") or 0), 4),
            "mean_confidence": round(float(stats.get(f"confidence_{key}") or 0), 4),
            "routes": _route_summary(stats.get(f"routes_{key}")),
        }

    return {
        "method": METHOD_VERSION,
        "fusion_mode": params["fusion_mode"],
        "parameters": {k: params[k] for k in ("q_min", "tau_change", "confidence_threshold", "optical_reference_count", "sar_reference_count", "scale")},
        "formulas": {
            "q_o": "Q_o = V_o x (1 - C_o) x A_o; C_o = median probabilitas awan observasi cerah",
            "q_s": "Q_s = V_s x S_s x A_s",
            "routing": "joint jika Q_o>=q_min dan Q_s>=q_min; optical/SAR jika hanya satu; no-data jika keduanya di bawah q_min",
            "weighted": "P = (Q_o x P_opt + Q_s x P_sar) / max(Q_o + Q_s, eps)",
            "change": "D = 1/2 x sum_k |P_t0,k - P_t1,k|; valid jika D>=tau_change, kelas berbeda, dan kedua periode lolos q_min",
        },
        "periods": {"t0": period_report("t0"), "t1": period_report("t1")},
        "statistics": {
            "aoi_area_ha": round(aoi_ha, 2),
            "valid_area_ha": round(valid_ha, 2),
            "no_data_area_ha": round(max(0.0, aoi_ha - valid_ha), 2),
            "changed_area_ha": round(changed_ha, 2),
            "stable_area_ha": round(valid_ha - changed_ha, 2),
            "suppressed_change_area_ha": round(float(areas.get("suppressed") or 0), 2),
            "changed_percentage": round(changed_ha / valid_ha * 100, 2),
            "mean_change_score": round(float(stats.get("d_mean") or 0), 4),
        },
        "flows": flows,
        "net_changes": net_changes,
        "tiles": tiles,
        "legends": {
            "route": ROUTE_LEGEND,
            "landcover": [{"value": int(code), **entry} for code, entry in legend.items()],
            "quality": {"min": 0, "max": 1, "palette": _QUALITY_PALETTE},
        },
        "resolution": f"{scale}m",
        "comparison_crs": "EPSG:6933",
        "training": training,
        "limitations": [
            "Q_o dan Q_s adalah indikator kelayakan input, bukan akurasi klasifikasi.",
            "Classifier dilatih dari label referensi MapBiomas 4.1 (30 m); akurasi independen belum tersedia.",
            "Probabilitas kelas adalah proporsi suara Random Forest, bukan probabilitas terkalibrasi.",
        ],
    }
