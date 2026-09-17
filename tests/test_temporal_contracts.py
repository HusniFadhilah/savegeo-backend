from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.temporal import format_rfc3339, parse_calendar_date, parse_rfc3339
from app.schemas.temporal import TemporalInterval


def test_offset_is_normalized_to_utc_with_milliseconds():
    value = parse_rfc3339("2026-09-17T15:15:30.125+07:00")
    assert format_rfc3339(value) == "2026-09-17T08:15:30.125Z"


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        parse_rfc3339("2026-09-17T15:15:30")
    with pytest.raises(ValueError, match="timezone"):
        parse_rfc3339(datetime(2026, 9, 17, 15, 15, 30))


def test_calendar_date_does_not_become_an_instant():
    assert parse_calendar_date("2026-09-17").isoformat() == "2026-09-17"
    with pytest.raises(ValueError, match="calendar date"):
        parse_calendar_date(datetime(2026, 9, 17, tzinfo=UTC))


def test_half_open_interval_requires_ordered_aware_timestamps():
    interval = TemporalInterval(
        start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00.000Z"
    )
    assert interval.model_dump()["start"] == "2026-09-01T00:00:00.000Z"
    assert interval.model_dump()["semantics"] == "[start,end)"
    with pytest.raises(ValidationError):
        TemporalInterval(start="2026-10-01T00:00:00Z", end="2026-09-01T00:00:00Z")
