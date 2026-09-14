"""Compare existing Dynamic World predictions without inventing disaster classes."""
import math

import ee

from app.registries.disaster_model_registry import get_model
from app.services.gee_common import AnalysisError, get_tile_url

PROBABILITY_BANDS = ["water", "trees", "grass", "flooded_vegetation", "crops", "shrub_and_scrub", "built", "bare", "snow_and_ice"]


def _prediction(aoi, date):
    # Exactly the configured acquisition date, not a silent +/- date fallback.
    day = ee.Date(date.isoformat())
    col = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterBounds(aoi).filterDate(day, day.advance(1, "day"))
    provenance = ee.Dictionary({"scene_ids": col.aggregate_array("system:index"),
        "algorithm_versions": col.aggregate_array("dynamicworld_algorithm_version").distinct(),
        "qa_versions": col.aggregate_array("qa_algorithm_version").distinct()}).getInfo()
    ids = provenance["scene_ids"]
    if not ids:
        raise AnalysisError(f"Prediksi Dynamic World tidak tersedia pada tanggal {date}; pilih imagery Sentinel-2 lain secara eksplisit", 404)
    image = col.sort("system:index").mosaic().clip(aoi)
    return image, {"date": date.isoformat(), **provenance, "scene_count": len(ids)}


def _groups(label, confidence, aoi, projection):
    values = ee.Image.pixelArea().divide(10000).rename("area").addBands(ee.Image.constant(1).rename("pixels")).addBands(confidence.rename("confidence")).addBands(label.rename("class_id"))
    return values.reduceRegion(reducer=ee.Reducer.sum().repeat(3).group(groupField=3, groupName="class_id"),
        geometry=aoi, crs=projection, scale=10, maxPixels=100_000_000, tileScale=4).getInfo().get("groups", [])


def class_statistics(schema, pre_groups, post_groups, aoi_area):
    if not math.isfinite(aoi_area) or aoi_area <= 0:
        raise AnalysisError("Luas AOI tidak valid", 422)
    sides = [{int(g["class_id"]): g["sum"] for g in groups} for groups in (pre_groups, post_groups)]
    valid_ids = {c["class_id"] for c in schema}
    for side in sides:
        if not side or not set(side).issubset(valid_ids):
            raise AnalysisError("Raster tidak memiliki kelas valid sesuai registry", 422)
        if any(not math.isfinite(float(v)) or v < 0 for sums in side.values() for v in sums):
            raise AnalysisError("Statistik raster tidak valid", 422)
        total = sum(s[0] for s in side.values())
        if total <= 0 or total > aoi_area * 1.005:
            raise AnalysisError("Luas kelas kosong atau melebihi AOI", 422)
    rows = []
    for c in schema:
        pre, post = [s.get(c["class_id"], [0, 0, 0]) for s in sides]
        rows.append({**c, "pre_area_ha": pre[0], "post_area_ha": post[0], "change_area_ha": post[0]-pre[0],
            "pre_pixel_count": round(pre[1]), "post_pixel_count": round(post[1]),
            "pre_percentage": pre[0]/aoi_area*100, "post_percentage": post[0]/aoi_area*100,
            "pre_confidence": pre[2]/pre[1] if pre[1] else None,
            "post_confidence": post[2]/post[1] if post[1] else None})
    return rows


def compute_segmentation(aoi, pre_date, post_date):
    model = get_model("dynamic_world_v1")
    pre, pre_meta = _prediction(aoi, pre_date)
    post, post_meta = _prediction(aoi, post_date)
    for key in ("algorithm_versions", "qa_versions"):
        if len(pre_meta[key]) != 1 or set(pre_meta[key]) != set(post_meta[key]):
            raise AnalysisError(f"Versi model/preprocessing pre/post tidak konsisten ({key}); pilih pasangan imagery lain", 422)
    # Categorical labels use nearest-neighbour on one explicit common grid.
    # The selected first pre scene supplies a real 10m UTM projection.
    projection = ee.Image(f"GOOGLE/DYNAMICWORLD/V1/{pre_meta['scene_ids'][0]}").select("label").projection()
    pre = pre.reproject(projection).clip(aoi)
    post = post.reproject(projection).clip(aoi)
    valid = pre.select("label").mask().And(post.select("label").mask())
    pre, post = pre.updateMask(valid), post.updateMask(valid)
    labels = [im.select("label").toInt() for im in (pre, post)]
    confidence = [im.select(PROBABILITY_BANDS).reduce(ee.Reducer.max()) for im in (pre, post)]
    aoi_area = aoi.area(1).divide(10000).getInfo()
    rows = class_statistics(model["classes"], *[_groups(l, c, aoi, projection) for l, c in zip(labels, confidence)], aoi_area)
    # Transition code uniquely identifies pre-class -> post-class, including unchanged.
    change = labels[0].multiply(9).add(labels[1]).rename("transition")
    groups = ee.Image.pixelArea().divide(10000).addBands(change).reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="transition"), geometry=aoi,
        crs=projection, scale=10, maxPixels=100_000_000, tileScale=4).getInfo().get("groups", [])
    transitions = []
    for g in groups:
        before, after = divmod(int(g["transition"]), 9)
        transitions.append({"pre_class_id": before, "post_class_id": after, "area_ha": g["sum"],
            "label": f"{model['classes'][before]['label']} → {model['classes'][after]['label']}",
            "color": model["classes"][after]["color"]})
    for row in rows:
        key = row["class_id"]
        row["gained_area_ha"] = sum(t["area_ha"] for t in transitions if t["post_class_id"] == key and t["pre_class_id"] != key)
        row["lost_area_ha"] = sum(t["area_ha"] for t in transitions if t["pre_class_id"] == key and t["post_class_id"] != key)
        row["unchanged_area_ha"] = sum(t["area_ha"] for t in transitions if t["pre_class_id"] == t["post_class_id"] == key)
    palette = [c["color"] for c in model["classes"]]
    images = {"pre": labels[0], "post": labels[1], "change": labels[1].updateMask(labels[0].neq(labels[1]))}
    tiles = {}
    for name, im in images.items():
        tile = get_tile_url(im.clip(aoi), {"min": 0, "max": 8, "palette": palette}, f"Dynamic World {name}")
        if not tile or not tile.get("tile_url"):
            raise AnalysisError(f"Tile segmentasi {name} gagal dibuat", 502)
        tiles[f"{name}_tile_url"] = tile["tile_url"]
    conf_tile = get_tile_url(confidence[1].clip(aoi), {"min": 0, "max": 1, "palette": ["#fff7bc", "#006837"]}, "Probabilitas kelas post")
    covered = sum(r["pre_area_ha"] for r in rows)
    review_reasons = []
    if covered / aoi_area < 0.5:
        review_reasons.append("Kurang dari 50% AOI memiliki piksel valid bersama; hasil perlu ditinjau")
    if any((r["pre_confidence"] is not None and r["pre_confidence"] < .5) or
           (r["post_confidence"] is not None and r["post_confidence"] < .5) for r in rows):
        review_reasons.append("Sebagian kelas memiliki probabilitas rata-rata di bawah 0,5; interpretasikan dengan citra referensi")
    comparison = {**tiles, "confidence_tile_url": conf_tile["tile_url"] if conf_tile else None,
        "classes": rows, "transitions": transitions, "pre": pre_meta, "post": post_meta,
        "resolution_m": 10, "crs": projection.crs().getInfo(), "aoi": aoi.getInfo(), "review_reasons": review_reasons,
        "method": model["description"], "model_id": model["model_id"], "model_version": model["version"],
        "aoi_area_ha": aoi_area, "valid_area_ha": covered, "no_data_area_ha": max(0, aoi_area-covered),
        "changed_area_ha": sum(t["area_ha"] for t in transitions if t["pre_class_id"] != t["post_class_id"]),
        "unchanged_area_ha": sum(t["area_ha"] for t in transitions if t["pre_class_id"] == t["post_class_id"]),
        "coverage_note": "Statistik memakai irisan piksel valid pre/post; awan dan no-data dikeluarkan pada kedua sisi. Perubahan warna menunjukkan kelas tujuan, bukan penyebab kerusakan."}
    return {"tile_url": tiles["post_tile_url"], "legend": model["classes"],
        "statistics": {"aoi_area_ha": aoi_area, "changed_area_ha": comparison["changed_area_ha"], "comparison": comparison},
        "confidence_summary": {"type": "top1_class_probability", "note": "Probabilitas kelas tutupan lahan, bukan confidence kerusakan bencana"}}
