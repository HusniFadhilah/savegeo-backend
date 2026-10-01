from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.api.routes import wildfire_events
from app.services import fire_multi_source_service as fire


def test_burn_period_helpers_handle_leap_year_and_year_boundary():
    start = date(2024, 12, 29)
    end = date(2025, 1, 5)
    assert fire._month_keys(start, end) == ["2024-12", "2025-01"]
    assert fire._calendar_year_segments(start, end) == [
        (2024, date(2024, 12, 29), date(2024, 12, 31)),
        (2025, date(2025, 1, 1), date(2025, 1, 5)),
    ]
    assert date(2024, 2, 29).timetuple().tm_yday == 60


def test_public_burned_area_ignores_stale_or_unpublished_analysis():
    event = SimpleNamespace(id=40)
    stale_run = SimpleNamespace(model_id="fire_burned_area_v1")
    stale_result = SimpleNamespace(
        statistics={"area_ha": 123.45},
        published_at=datetime.now(timezone.utc),
    )
    with (
        patch.object(wildfire_events.disaster_model_registry, "get_model", return_value={"enabled": True}),
        patch.object(
            wildfire_events.disaster_repo,
            "list_published_analyses",
            return_value=[(stale_run, stale_result)],
        ),
        patch.object(
            wildfire_events.disaster_capability_service,
            "validate_persisted_result",
            return_value=SimpleNamespace(allowed=False),
        ),
    ):
        assert wildfire_events._published_burned_area(None, event) is None


def test_public_burned_area_reads_only_compatible_published_statistics():
    event = SimpleNamespace(id=40)
    run = SimpleNamespace(model_id="fire_burned_area_v1", completed_at=datetime.now(timezone.utc))
    result = SimpleNamespace(
        statistics={
            "area_ha": 123.456,
            "product": "NASA MODIS MCD64A1",
            "period": {"start": "2026-01-01", "end": "2026-01-15"},
            "coverage": "complete",
            "unit": "ha",
            "resolution_m": 500,
        },
        published_at=None,
    )
    with (
        patch.object(wildfire_events.disaster_model_registry, "get_model", return_value={"enabled": True}),
        patch.object(
            wildfire_events.disaster_repo,
            "list_published_analyses",
            return_value=[(run, result)],
        ),
        patch.object(
            wildfire_events.disaster_capability_service,
            "validate_persisted_result",
            return_value=SimpleNamespace(allowed=True),
        ),
    ):
        area = wildfire_events._published_burned_area(None, event)

    assert area["area_ha"] == 123.46
    assert area["source"] == "NASA MODIS MCD64A1"
    assert area["resolution_m"] == 500
