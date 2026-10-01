from app.services.disaster_exposure_service import compute_building_exposure, compute_building_segmentation_candidate, compute_road_exposure


AOI = {
    "type": "Feature",
    "properties": {},
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]},
}


def feature(geometry, properties=None):
    return {"type": "Feature", "properties": properties or {}, "geometry": geometry}


def params(features, hazard, **extra):
    return {
        "exposure_features": {"type": "FeatureCollection", "features": features},
        "hazard_geojson": hazard,
        "source_dataset": "test-dataset",
        "source_date": "2026-01-01",
        "source_kind": "local_upload",
        **extra,
    }


def test_building_exposure_intersection_is_clipped_and_classified():
    buildings = [
        feature({"type": "Polygon", "coordinates": [[[0.001, 0.001], [0.003, 0.001], [0.003, 0.003], [0.001, 0.003], [0.001, 0.001]]]}, {"id": "b1", "district": "A"}),
        feature({"type": "Polygon", "coordinates": [[[0.008, 0.008], [0.012, 0.008], [0.012, 0.012], [0.008, 0.012], [0.008, 0.008]]]}, {"id": "b2", "district": "B"}),
    ]
    hazard = {"type": "Polygon", "coordinates": [[[0, 0], [0.002, 0], [0.002, 0.01], [0, 0.01], [0, 0]]]}
    result = compute_building_exposure(AOI, params(buildings, hazard))
    assert result["statistics"]["buildings_exposed"] == 1
    assert result["statistics"]["exposed_building_footprint_area_ha"] > 0
    assert all(feature["geometry"] for feature in result["features"]["features"])
    assert all(feature["properties"]["semantics"] == "building_exposure" for feature in result["features"]["features"])


def test_road_exposure_deduplicates_overlapping_segments():
    roads = [
        feature({"type": "LineString", "coordinates": [[0.001, 0.005], [0.009, 0.005]]}, {"highway": "primary"}),
        feature({"type": "LineString", "coordinates": [[0.001, 0.005], [0.009, 0.005]]}, {"highway": "primary"}),
    ]
    hazard = {"type": "Polygon", "coordinates": [[[0, 0], [0.005, 0], [0.005, 0.01], [0, 0.01], [0, 0]]]}
    result = compute_road_exposure(AOI, params(roads, hazard))
    assert result["statistics"]["road_segments_exposed"] == 1
    assert result["statistics"]["road_length_exposed_m"] > 0
    assert result["quality_flags"]["overlap_deduplicated"] is True


def test_building_segmentation_is_persisted_as_candidate_not_damage():
    candidate = feature(
        {"type": "Polygon", "coordinates": [[[0.001, 0.001], [0.003, 0.001], [0.003, 0.003], [0.001, 0.003], [0.001, 0.001]]]},
        {"segment_id": 7, "confidence": 0.82},
    )
    result = compute_building_segmentation_candidate(AOI, {"features": {"type": "FeatureCollection", "features": [candidate]}, "hazard_geojson": AOI, "source_dataset": "samgeo", "source_date": "2026-01-01", "source_kind": "local_upload"})
    assert result["statistics"]["building_candidates"] == 1
    assert result["quality_flags"]["candidate_only"] is True
    assert result["features"]["features"][0]["properties"]["semantics"] == "building_footprint_candidate"
