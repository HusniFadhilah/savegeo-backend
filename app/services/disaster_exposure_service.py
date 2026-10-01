"""Deterministic vector exposure calculations for persisted disaster runs.

The service deliberately accepts explicit GeoJSON inputs instead of silently
fetching OSM or inventing a footprint layer.  This keeps the result auditable:
the caller must declare the hazard geometry, exposure dataset, source date,
and source kind in the run parameters.  All measurements use WGS84 geodesic
area/length calculations and every output feature is clipped to the AOI.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from pyproj import Geod
from shapely.geometry import GeometryCollection, LineString, MultiLineString, mapping, shape
from shapely.ops import unary_union
from shapely.validation import make_valid

from app.services.gee_common import AnalysisError

_GEOD = Geod(ellps="WGS84")
_BUILDING_CLASSES = ("low_overlap", "medium_overlap", "high_overlap")


def _geometry(value: dict[str, Any] | None):
    if not value:
        raise AnalysisError("Geometri GeoJSON tidak tersedia", 422)
    geojson = value
    if value.get("type") == "Feature":
        geojson = value.get("geometry")
    elif value.get("type") == "FeatureCollection":
        return unary_union([_geometry(feature) for feature in value.get("features", [])])
    if not geojson or not geojson.get("type"):
        raise AnalysisError("GeoJSON tidak memiliki geometry", 422)
    geometry = make_valid(shape(geojson))
    if geometry.is_empty:
        raise AnalysisError("GeoJSON memiliki geometry kosong", 422)
    return geometry


def _features(value: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if isinstance(value, list):
        features = value
    elif isinstance(value, dict) and value.get("type") == "FeatureCollection":
        features = value.get("features", [])
    elif isinstance(value, dict) and value.get("type") == "Feature":
        features = [value]
    else:
        raise AnalysisError("Dataset exposure harus berupa GeoJSON FeatureCollection", 422)
    if not features:
        raise AnalysisError("Dataset exposure tidak memiliki feature", 422)
    result = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise AnalysisError("Feature exposure tidak valid", 422)
        _geometry(feature)
        result.append(feature)
    return result


def _area_m2(geometry) -> float:
    if geometry.is_empty:
        return 0.0
    area, _ = _GEOD.geometry_area_perimeter(geometry)
    return abs(float(area))


def _length_m(geometry) -> float:
    if geometry.is_empty:
        return 0.0
    return float(_GEOD.geometry_length(geometry))


def _feature_collection(features: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


def _aoi_geometry(aoi_geojson: dict[str, Any]):
    aoi = _geometry(aoi_geojson)
    if aoi.geom_type not in {"Polygon", "MultiPolygon"}:
        raise AnalysisError("AOI harus berupa polygon", 422)
    return aoi


def _hazard_geometry(parameters: dict[str, Any]):
    hazard = parameters.get("hazard_geojson") or parameters.get("flood_geojson")
    if hazard is None:
        hazard = parameters.get("hazard_features") or parameters.get("flood_features")
    if hazard is None:
        raise AnalysisError("Geometri genangan wajib disediakan; tidak ada fallback statis", 422)
    return _geometry(hazard)


def _source_metadata(parameters: dict[str, Any]) -> dict[str, Any]:
    dataset = parameters.get("source_dataset") or parameters.get("source_datasets")
    source_date = parameters.get("source_date") or parameters.get("acquisition_date")
    source_kind = parameters.get("source_kind")
    if not dataset or not source_date or not source_kind:
        raise AnalysisError("source_dataset, source_date, dan source_kind wajib untuk exposure", 422)
    return {"source_dataset": dataset, "source_date": source_date, "source_kind": source_kind}


def _region_key(properties: dict[str, Any]) -> str:
    return str(properties.get("district") or properties.get("kecamatan") or properties.get("province") or "unknown")


def compute_building_exposure(aoi_geojson: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """Intersect buildings with an explicit flood/inundation geometry."""
    source = _source_metadata(parameters)
    aoi = _aoi_geometry(aoi_geojson)
    hazard = _hazard_geometry(parameters).intersection(aoi)
    buildings = _features(parameters.get("exposure_features") or parameters.get("building_features"))
    output = []
    by_region: dict[str, dict[str, Any]] = defaultdict(lambda: {"buildings_exposed": 0, "footprint_area_ha": 0.0})
    total_buildings = 0
    total_exposed_area = 0.0
    for index, feature in enumerate(buildings):
        footprint = _geometry(feature).intersection(aoi)
        if footprint.is_empty or footprint.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        total_buildings += 1
        footprint_area = _area_m2(footprint)
        overlap = footprint.intersection(hazard)
        overlap_area = _area_m2(overlap)
        if overlap_area <= 0:
            continue
        ratio = min(1.0, overlap_area / footprint_area) if footprint_area else 0.0
        exposure_class = "low_overlap" if ratio < 0.25 else "medium_overlap" if ratio < 0.5 else "high_overlap"
        properties = dict(feature.get("properties") or {})
        properties.update({
            "source_feature_id": properties.get("id", index),
            "exposure_class": exposure_class,
            "overlap_area_ha": round(overlap_area / 10_000, 6),
            "overlap_pct": round(ratio * 100, 3),
            "semantics": "building_exposure",
        })
        output.append({"type": "Feature", "geometry": mapping(footprint), "properties": properties})
        region = by_region[_region_key(properties)]
        region["buildings_exposed"] += 1
        region["footprint_area_ha"] += overlap_area / 10_000
        total_exposed_area += overlap_area
    if not output:
        raise AnalysisError("Tidak ada footprint bangunan yang beririsan dengan genangan", 404)
    counts = {key: sum(1 for feature in output if feature["properties"]["exposure_class"] == key) for key in _BUILDING_CLASSES}
    return {
        "features": _feature_collection(output),
        "statistics": {
            "buildings_exposed": len(output),
            "building_count_in_aoi": total_buildings,
            "exposed_building_footprint_area_ha": round(total_exposed_area / 10_000, 6),
            "exposed_building_pct": round(len(output) / total_buildings * 100, 3) if total_buildings else None,
            "by_overlap_class": counts,
            "by_region": {key: {**value, "footprint_area_ha": round(value["footprint_area_ha"], 6)} for key, value in by_region.items()},
            "aoi_area_ha": round(_area_m2(aoi) / 10_000, 6),
        },
        "legend": [
            {"label": "Low overlap", "color": "#ffeb3b", "value": "low_overlap"},
            {"label": "Medium overlap", "color": "#ff9800", "value": "medium_overlap"},
            {"label": "High overlap", "color": "#d32f2f", "value": "high_overlap"},
        ],
        "quality_flags": {"source_declared": True, "geometry_clipped_to_aoi": True, "damage_not_inferred": True},
        "coverage": {"valid_area_ha": round(_area_m2(aoi) / 10_000, 6), "no_data_area_ha": 0.0, "coverage_pct": 100.0},
        "provenance_source": source,
    }


def _line_parts(geometry) -> list[Any]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [part for part in geometry.geoms if isinstance(part, (LineString, MultiLineString)) for part in _line_parts(part)]
    return []


def compute_road_exposure(aoi_geojson: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """Intersect road segments and deduplicate overlap before total length."""
    source = _source_metadata(parameters)
    aoi = _aoi_geometry(aoi_geojson)
    hazard = _hazard_geometry(parameters).intersection(aoi)
    roads = _features(parameters.get("exposure_features") or parameters.get("road_features"))
    intersections: list[tuple[Any, dict[str, Any]]] = []
    by_class: dict[str, list[Any]] = defaultdict(list)
    for index, feature in enumerate(roads):
        clipped = _geometry(feature).intersection(aoi).intersection(hazard)
        lines = _line_parts(clipped)
        properties = dict(feature.get("properties") or {})
        road_class = str(properties.get("road_class") or properties.get("class") or properties.get("highway") or "unknown")
        for line in lines:
            if not line.is_empty and _length_m(line) > 0:
                intersections.append((line, {**properties, "source_feature_id": properties.get("id", index), "road_class": road_class}))
                by_class[road_class].append(line)
    if not intersections:
        raise AnalysisError("Tidak ada segmen jalan yang beririsan dengan genangan", 404)
    unique_union = unary_union([line for line, _ in intersections])
    unique_lines = _line_parts(unique_union)
    output = []
    for index, line in enumerate(unique_lines):
        output.append({"type": "Feature", "geometry": mapping(line), "properties": {
            "segment_id": index + 1, "length_m": round(_length_m(line), 3),
            "road_class": "deduplicated", "semantics": "road_exposure",
        }})
    return {
        "features": _feature_collection(output),
        "statistics": {
            "road_segments_exposed": len(output),
            "road_length_exposed_m": round(_length_m(unique_union), 3),
            "road_length_exposed_km": round(_length_m(unique_union) / 1000, 6),
            "length_by_class_m": {key: round(_length_m(unary_union(lines)), 3) for key, lines in by_class.items()},
            "aoi_area_ha": round(_area_m2(aoi) / 10_000, 6),
        },
        "legend": [{"label": "Road exposure", "color": "#7b1fa2", "value": "road_exposure"}],
        "quality_flags": {"source_declared": True, "geometry_clipped_to_aoi": True, "overlap_deduplicated": True, "damage_not_inferred": True},
        "coverage": {"valid_area_ha": round(_area_m2(aoi) / 10_000, 6), "no_data_area_ha": 0.0, "coverage_pct": 100.0},
        "provenance_source": source,
    }


def compute_landcover_impact(aoi_geojson: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    source = _source_metadata(parameters)
    aoi = _aoi_geometry(aoi_geojson)
    hazard = _hazard_geometry(parameters).intersection(aoi)
    rows: dict[str, float] = defaultdict(float)
    output = []
    for feature in _features(parameters.get("landcover_features")):
        geometry = _geometry(feature).intersection(aoi).intersection(hazard)
        if geometry.is_empty:
            continue
        properties = feature.get("properties") or {}
        class_name = properties.get("class") or properties.get("label") or properties.get("class_name")
        if not class_name:
            raise AnalysisError("Feature tutupan lahan tidak memiliki atribut class/label", 422)
        class_name = str(class_name)
        area_ha = _area_m2(geometry) / 10_000
        rows[class_name] += area_ha
        output.append({"type": "Feature", "geometry": mapping(geometry), "properties": {
            **properties, "class": class_name, "affected_area_ha": round(area_ha, 6), "semantics": "landcover_impact",
        }})
    if not rows:
        raise AnalysisError("Tidak ada tutupan lahan yang beririsan dengan genangan", 404)
    total = sum(rows.values())
    return {
        "features": _feature_collection(output),
        "statistics": {"affected_area_ha": round(total, 6), "by_class": {key: round(value, 6) for key, value in rows.items()},
                       "aoi_area_ha": round(_area_m2(aoi) / 10_000, 6),
                       "by_class_pct_aoi": {key: round(value / (_area_m2(aoi) / 10_000) * 100, 6) for key, value in rows.items()}},
        "legend": [{"label": key, "color": "#607d8b", "value": key} for key in sorted(rows)],
        "quality_flags": {"source_declared": True, "geometry_clipped_to_aoi": True, "economic_damage_not_inferred": True},
        "coverage": {"valid_area_ha": round(_area_m2(aoi) / 10_000, 6), "no_data_area_ha": 0.0, "coverage_pct": 100.0},
        "provenance_source": source,
    }


def compute_building_segmentation_candidate(aoi_geojson: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    """Persist an externally produced SamGeo/SAM footprint as a candidate.

    Inference remains in the SamGeo job service.  This function is the
    auditable hand-off into AnalysisResult; it never upgrades a segmentation
    mask into a damage label.
    """
    source = _source_metadata(parameters)
    aoi = _aoi_geometry(aoi_geojson)
    candidates = _features(parameters.get("features") or parameters.get("segmentation_features"))
    output = []
    areas = []
    confidences = []
    for index, feature in enumerate(candidates):
        geometry = _geometry(feature).intersection(aoi)
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        properties = dict(feature.get("properties") or {})
        confidence = properties.get("confidence")
        if isinstance(confidence, (int, float)):
            confidences.append(float(confidence))
        properties.update({"candidate_id": properties.get("segment_id", index + 1), "semantics": "building_footprint_candidate"})
        output.append({"type": "Feature", "geometry": mapping(geometry), "properties": properties})
        areas.append(_area_m2(geometry) / 10_000)
    if not output:
        raise AnalysisError("Tidak ada polygon footprint kandidat valid di dalam AOI", 422)
    return {
        "features": _feature_collection(output),
        "statistics": {"building_candidates": len(output), "candidate_footprint_area_ha": round(sum(areas), 6),
                        "mean_confidence": round(sum(confidences) / len(confidences), 6) if confidences else None,
                        "aoi_area_ha": round(_area_m2(aoi) / 10_000, 6)},
        "legend": [{"label": "Building footprint candidate", "color": "#7c3aed", "value": "building_footprint_candidate"}],
        "quality_flags": {"source_declared": True, "geometry_clipped_to_aoi": True, "candidate_only": True, "damage_not_inferred": True},
        "coverage": {"valid_area_ha": round(_area_m2(aoi) / 10_000, 6), "no_data_area_ha": 0.0, "coverage_pct": 100.0},
        "review_reasons": ["Footprint berasal dari segmentasi kandidat; validasi manual wajib sebelum publikasi."],
        "provenance_source": source,
    }
