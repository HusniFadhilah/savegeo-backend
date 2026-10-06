from __future__ import annotations

import json
import traceback
from types import SimpleNamespace

import pytest

from app.services import firms_service


CSV = """latitude,longitude,bright_ti4,bright_ti5,scan,track,acq_date,acq_time,satellite,instrument,confidence,frp,daynight
-6.123,106.456,330.2,295.4,0.4,0.3,2026-09-15,0430,N20,VIIRS,h,42.7,D
91,106.0,300,290,0.4,0.3,2026-09-15,0430,N20,VIIRS,n,2,N
-6.2,106.5,320,290,0.4,0.3,2026-09-15,0431,N20,VIIRS,45,5,N
"""


def settings(map_key: str = "test-map-key"):
    return SimpleNamespace(
        nasa_firms_map_key=map_key,
        nasa_firms_base_url="https://firms.modaps.eosdis.nasa.gov",
        nasa_firms_cache_ttl_seconds=900,
        nasa_firms_request_timeout_seconds=20,
        nasa_firms_max_day_range=7,
    )


def test_normalize_csv_coordinates_confidence_and_stable_id():
    first = firms_service.normalize_firms_csv(CSV, "VIIRS_NOAA20_NRT")
    second = firms_service.normalize_firms_csv(CSV, "VIIRS_NOAA20_NRT")

    assert len(first) == 2
    assert first[0]["id"] == second[0]["id"]
    assert first[0]["geometry"]["coordinates"] == [106.456, -6.123]
    assert first[0]["properties"]["confidence"] == "h"
    assert first[0]["properties"]["confidence_label"] == "high"
    assert first[0]["properties"]["frp"] == 42.7
    assert first[0]["properties"]["acq_datetime_utc"] == "2026-09-15T04:30:00Z"


def test_validation_rejects_global_or_invalid_queries():
    with pytest.raises(firms_service.FirmsRequestError):
        firms_service.validate_bbox((-180, -90, 180, 90))
    with pytest.raises(firms_service.FirmsRequestError):
        firms_service.validate_query("unknown", 1, None, None, None, 100)
    with pytest.raises(firms_service.FirmsRequestError):
        firms_service.validate_query("all", 5, None, None, None, 100)
    with pytest.raises(firms_service.FirmsRequestError):
        firms_service.validate_query("VIIRS_NOAA20_NRT", 1, None, None, None, firms_service.MAX_PERIOD_LIMIT)


def test_get_fires_uses_cache_and_never_returns_map_key(monkeypatch):
    firms_service.clear_cache()
    monkeypatch.setattr(firms_service, "get_settings", lambda: settings())
    calls = []

    class Response:
        status_code = 200
        text = CSV
        headers = {"content-type": "text/csv"}

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(firms_service.requests, "get", fake_get)
    query = {
        "source": "VIIRS_NOAA20_NRT",
        "day_range": 1,
        "requested_date": "2026-09-15",
        "bbox": (106, -7, 107, -6),
        "min_confidence": "nominal",
        "min_frp": 20,
        "limit": 100,
    }
    payload = firms_service.get_fires(**query)
    cached = firms_service.get_fires(**query)

    assert len(calls) == 1
    assert payload["metadata"]["count"] == 1
    assert cached["metadata"]["cached"] is True
    assert "test-map-key" not in json.dumps(payload)
    assert "test-map-key" not in json.dumps(cached)


def test_get_fires_splits_ranges_longer_than_nasa_limit(monkeypatch):
    firms_service.clear_cache()
    monkeypatch.setattr(firms_service, "get_settings", lambda: settings())
    calls = []

    class Response:
        status_code = 200
        text = CSV
        headers = {"content-type": "text/csv"}

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(firms_service.requests, "get", fake_get)
    result = firms_service.get_fires(
        source="VIIRS_NOAA20_NRT",
        day_range=7,
        requested_date="2026-09-15",
        bbox=(106, -7, 107, -6),
        min_confidence=None,
        min_frp=None,
        limit=100,
    )

    assert len(calls) == 2
    assert "/5/2026-09-15" in calls[0][0]
    assert "/2/2026-09-20" in calls[1][0]
    assert result["metadata"]["returned_count"] == 4


def test_get_fires_for_period_filters_sensor_and_confidence(monkeypatch):
    firms_service.clear_cache()
    monkeypatch.setattr(firms_service, "get_settings", lambda: settings())

    class Response:
        status_code = 200
        text = CSV
        headers = {"content-type": "text/csv"}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(firms_service.requests, "get", lambda url, **kwargs: Response())
    result = firms_service.get_fires_for_period(
        source="VIIRS_NOAA20_NRT",
        from_date="2026-09-15",
        to_date="2026-09-15",
        bbox=(106, -7, 107, -6),
        confidence_categories={"high"},
        sensor="viirs",
        min_frp=None,
        limit=firms_service.MAX_PERIOD_LIMIT,
    )

    assert result["metadata"]["count"] == 1
    assert result["metadata"]["summary"]["high_confidence_hotspots"] == 1


@pytest.mark.parametrize("request_kind", ["area", "wms"])
@pytest.mark.parametrize("exception_type", [firms_service.requests.ConnectionError, firms_service.requests.Timeout])
def test_upstream_request_error_traceback_never_contains_map_key(monkeypatch, request_kind, exception_type):
    key = "test-only-secret-map-key"
    monkeypatch.setattr(firms_service, "get_settings", lambda: settings(key))
    monkeypatch.setattr(firms_service.time, "sleep", lambda *_: None)
    firms_service.clear_cache()

    def raise_with_url(url, **kwargs):
        raise exception_type(f"connection failed: {url}")

    monkeypatch.setattr(firms_service.requests, "get", raise_with_url)
    with pytest.raises(firms_service.FirmsRequestError) as captured:
        if request_kind == "area":
            firms_service._fetch_source_uncached(
                "VIIRS_NOAA20_NRT", (106, -7, 107, -6), 1, None
            )
        else:
            firms_service.fetch_wms("viirs_24h", (106, -7, 107, -6), "EPSG:4326", 256, 256, "image/png")

    rendered = "".join(traceback.format_exception(captured.type, captured.value, captured.tb))
    assert key not in str(captured.value)
    assert key not in rendered
    assert captured.value.__suppress_context__ is True
