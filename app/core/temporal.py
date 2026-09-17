"""Strict temporal primitives used by SaveGeo API contracts.

Dates are calendar values. Datetimes are absolute instants and must carry an
explicit offset. API serialization is normalized to UTC with millisecond
precision and a trailing ``Z``.
"""

from __future__ import annotations

import datetime as dt
import re

RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
CALENDAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def require_aware(value: dt.datetime, *, field: str = "timestamp") -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone offset")
    return value


def parse_rfc3339(value: object, *, field: str = "timestamp") -> dt.datetime:
    """Parse an RFC 3339 instant and return a timezone-aware UTC datetime."""
    if isinstance(value, dt.datetime):
        parsed = require_aware(value, field=field)
    elif isinstance(value, str) and RFC3339_TIMESTAMP.fullmatch(value):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be an RFC 3339 timestamp with timezone")
    return parsed.astimezone(dt.UTC)


def format_rfc3339(value: dt.datetime, *, field: str = "timestamp") -> str:
    """Serialize an aware datetime as canonical ``YYYY-MM-DDTHH:mm:ss.SSSZ``."""
    normalized = parse_rfc3339(value, field=field)
    milliseconds = normalized.microsecond // 1000
    return normalized.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def parse_calendar_date(value: str | dt.date, *, field: str = "date") -> dt.date:
    if isinstance(value, dt.datetime):
        raise ValueError(f"{field} must be a calendar date, not a timestamp")
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not CALENDAR_DATE.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def format_temporal(value: dt.date | dt.datetime | None, *, field: str = "temporal") -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return format_rfc3339(value, field=field)
    return value.isoformat()
