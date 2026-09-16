from __future__ import annotations

import json
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
