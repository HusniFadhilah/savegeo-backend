import pytest

from app.services.dmrv.boundary import (
    MAX_BOUNDARY_AREA_HA,
    BoundaryError,
    overlap_summary,
    validate_boundary,
)


def square(lon, lat, size=0.01):
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat],
        ]],
    }


def code_of(geojson) -> str:
    with pytest.raises(BoundaryError) as info:
        validate_boundary(geojson)
    return info.value.code


def test_valid_square_has_geodesic_area_and_stable_checksum():
    result = validate_boundary(square(110.0, -7.0))
    assert result.area_ha == pytest.approx(122.2, rel=0.02)
    assert result.area_method == "geodesic_wgs84_pyproj"
    assert result.hole_count == 0
    assert len(result.checksum) == 64
    reordered = {"coordinates": square(110.0, -7.0)["coordinates"], "type": "Polygon"}
    assert validate_boundary(reordered).checksum == result.checksum


def test_feature_and_single_feature_collection_are_accepted():
    geometry = square(110.0, -7.0)
    feature = {"type": "Feature", "properties": {}, "geometry": geometry}
    collection = {"type": "FeatureCollection", "features": [feature]}
    base = validate_boundary(geometry).checksum
    assert validate_boundary(feature).checksum == base
    assert validate_boundary(collection).checksum == base


def test_bowtie_is_rejected_not_repaired():
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    assert code_of(bowtie) == "invalid_geometry"


def test_antimeridian_crossing_ring_is_rejected():
    ring = [[179.9, 0], [-179.9, 0], [-179.9, 1], [179.9, 1], [179.9, 0]]
    assert code_of({"type": "Polygon", "coordinates": [ring]}) == "antimeridian"


def test_out_of_range_latitude_is_rejected():
    assert code_of(square(110.0, 91.0)) == "bad_coordinates"
    assert code_of(square(181.0, 0.0)) == "bad_coordinates"


def test_unsupported_and_empty_inputs():
    assert code_of({"type": "Point", "coordinates": [110.0, -7.0]}) == "unsupported_type"
    assert code_of({"type": "GeometryCollection", "geometries": []}) == "unsupported_type"
    assert code_of({"type": "FeatureCollection", "features": []}) == "empty"
    assert code_of({"type": "Polygon", "coordinates": []}) == "empty"


def test_two_features_are_rejected():
    feature = {"type": "Feature", "properties": {}, "geometry": square(110.0, -7.0)}
    other = {"type": "Feature", "properties": {}, "geometry": square(111.0, -7.0)}
    assert code_of({"type": "FeatureCollection", "features": [feature, other]}) == "too_many_features"


def test_area_over_limit_is_rejected():
    assert MAX_BOUNDARY_AREA_HA == 500_000.0
    assert code_of(square(100.0, -5.0, size=5.0)) == "too_large"


def test_hole_is_counted_and_reduces_area():
    outer = [[110.0, -7.0], [110.02, -7.0], [110.02, -6.98], [110.0, -6.98], [110.0, -7.0]]
    hole = [[110.005, -6.995], [110.015, -6.995], [110.015, -6.985], [110.005, -6.985], [110.005, -6.995]]
    with_hole = validate_boundary({"type": "Polygon", "coordinates": [outer, hole]})
    without = validate_boundary({"type": "Polygon", "coordinates": [outer]})
    assert with_hole.hole_count == 1
    assert with_hole.area_ha < without.area_ha * 0.9


def test_z_coordinates_are_accepted_and_dropped():
    ring = [[110.0, -7.0, 25.0], [110.01, -7.0, 26.0], [110.01, -6.99, 27.0], [110.0, -6.99, 28.0], [110.0, -7.0, 25.0]]
    result = validate_boundary({"type": "Polygon", "coordinates": [ring]})
    assert all(len(point) == 2 for point in result.validated_geojson["coordinates"][0])


def test_overlap_summary_counts_positive_area_only():
    base = validate_boundary(square(110.0, -7.0))
    half = square(110.005, -7.0)
    count, overlap_ha = overlap_summary(base.validated_geojson, [half])
    assert count == 1
    assert overlap_ha == pytest.approx(base.area_ha / 2, rel=0.02)
    assert overlap_summary(base.validated_geojson, [square(111.0, -7.0)]) == (0, 0.0)
    assert overlap_summary(base.validated_geojson, [square(110.01, -7.0)]) == (0, 0.0)
