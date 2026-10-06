from types import SimpleNamespace
from unittest.mock import patch

from app.api.routes import wildfire_events


def test_region_statistics_route_is_registered():
    assert any(
        route.path == "/disasters/wildfires/events/{slug}/region-statistics"
        for route in wildfire_events.router.routes
    )


def test_region_statistics_returns_a_region_summary_using_documented_filters():
    event = SimpleNamespace(province_codes=["62"], province=["Kalimantan Tengah"])
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [113, -1]}, "properties": {}}]
    region_features = [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}, "properties": {}}]
    expected_regions = [{"code": "62", "name": "Kalimantan Tengah", "hotspot_count": 1, "high_confidence_count": 1}]
    db = object()

    with (
        patch.object(wildfire_events, "_event", return_value=event),
        patch.object(wildfire_events, "_stored_wildfire_data", return_value=(features, {}, {})) as stored_data,
        patch.object(wildfire_events, "_admin_child_features", return_value=region_features),
        patch.object(wildfire_events.wildfire_region_service, "summarize_by_region", return_value=expected_regions),
    ):
        response = wildfire_events.get_wildfire_region_statistics(
            slug="kalimantan-2026",
            level="province",
            from_date="2026-08-20",
            to="2026-09-20",
            confidence="low,nominal,high",
            viewer=object(),
            db=db,
        )

    stored_data.assert_called_once_with(
        db,
        event,
        "2026-08-20",
        "2026-09-20",
        None,
        "low,nominal,high",
    )
    assert response == {
        "level": "province",
        "parent_code": None,
        "total_hotspots": 1,
        "regions": expected_regions,
        "attribution": "NASA FIRMS / NASA EOSDIS LANCE; batas administratif SP3STAB",
    }
