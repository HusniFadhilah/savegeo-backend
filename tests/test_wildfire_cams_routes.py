from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.api.routes import wildfire_events


@pytest.fixture(autouse=True)
def isolate_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(wildfire_events.cams_archive_service, "event_directory", lambda slug: tmp_path / slug)


def test_cams_capabilities_route_is_registered():
    paths = {route.path for route in wildfire_events.router.routes}
    assert "/disasters/wildfires/events/{slug}/emissions/capabilities" in paths
    assert "/disasters/wildfires/events/{slug}/emissions/summary" in paths
    assert wildfire_events.router.dependencies


def test_cams_capabilities_requires_a_published_wildfire_event():
    with (
        patch.object(
            wildfire_events, "_event", side_effect=HTTPException(404, "not published")
        ) as event_lookup,
        patch.object(wildfire_events.cams_emissions_service, "get_capabilities") as capabilities,
        pytest.raises(HTTPException) as captured,
    ):
        wildfire_events.get_wildfire_emissions_capabilities(
            "draft-fire", response=Response(), viewer=object(), db=object()
        )

    assert captured.value.status_code == 404
    event_lookup.assert_called_once()
    capabilities.assert_not_called()


def test_cams_capabilities_response_never_contains_api_key():
    payload = {"status": "needs_credentials", "data_available": False}
    response = Response()
    with (
        patch.object(wildfire_events, "_event", return_value=SimpleNamespace(status="published")),
        patch.object(wildfire_events.cams_emissions_service, "get_capabilities", return_value=payload),
    ):
        result = wildfire_events.get_wildfire_emissions_capabilities(
            "kalimantan-2026", response=response, viewer=object(), db=object()
        )
    assert result == payload
    assert "api_key" not in result
    assert response.headers["Cache-Control"] == "private, no-store"


def test_cams_summary_returns_explicit_unavailable_not_zero():
    with (
        patch.object(wildfire_events, "_event", return_value=SimpleNamespace(status="published")),
        patch.object(
            wildfire_events.cams_emissions_service,
            "get_capabilities",
            return_value={"blocked_reason": "ads_credentials_missing"},
        ),
        pytest.raises(HTTPException) as captured,
    ):
        wildfire_events.get_wildfire_emissions_summary(
            "kalimantan-2026",
            product="gfas",
            version="1.4.2",
            variable="co2",
            valid_at="2026-10-06T00:00:00Z",
            response=Response(),
            viewer=object(),
            db=object(),
        )

    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "ads_credentials_missing"
    assert "nol" in captured.value.detail["message"]
    assert captured.value.detail["request"]["variable"] == "co2"
    assert captured.value.headers["Cache-Control"] == "private, no-store"


def test_cams_summary_rejects_invalid_product_before_provider_access():
    with (
        patch.object(wildfire_events, "_event", return_value=SimpleNamespace(status="published")),
        patch.object(wildfire_events.cams_emissions_service, "get_capabilities") as capabilities,
        pytest.raises(HTTPException) as captured,
    ):
        wildfire_events.get_wildfire_emissions_summary(
            "kalimantan-2026",
            product="other",
            version="current",
            variable="pm25",
            valid_at="2026-10-06T00:00:00Z",
            response=Response(),
            viewer=object(),
            db=object(),
        )
    assert captured.value.status_code == 400
    assert captured.value.detail["code"] == "unsupported_product"
    capabilities.assert_not_called()
