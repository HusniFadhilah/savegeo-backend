from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import commercial_imagery_service as commercial
from app.services import imagery_service as imagery
from app.services.gee_common import AnalysisError


def test_commercial_catalog_keeps_planet_open_data_separate_and_discloses_provider_state(monkeypatch):
    monkeypatch.setattr(imagery, "commercial_provider_meta", lambda key: commercial.provider_meta(key, Settings()))
    catalog = imagery.list_providers()["providers"]

    assert "planet_open_data" in catalog
    assert catalog["planet_open_data"]["source_kind"] == "planet_open_data_stac"
    assert catalog["planet_commercial"]["source_kind"] == "planet_stac"
    assert catalog["airbus_oneatlas"]["capabilities"]["preview_kind"] == "provider_tiles"
    assert catalog["blacksky_spectra"]["capabilities"]["searchable"] is False
    assert catalog["blacksky_spectra"]["configuration_status"] == "not_configured"
    assert catalog["airbus_oneatlas"]["resolution_m"] is None


def test_configuration_requires_collection_and_never_returns_provider_secrets():
    incomplete = Settings(planet_stac_url="https://catalog.example/stac", planet_api_key="planet-secret")
    assert commercial.configuration("planet_commercial", incomplete)["configured"] is False

    ready = Settings(
        planet_stac_url="https://catalog.example/stac",
        planet_stac_collection="customer-collection",
        planet_api_key="planet-secret",
        oneatlas_api_key="airbus-secret",
        oneatlas_workspace_id="workspace-7",
        blacksky_spectra_api_contract_reference="contract-ticket-1",
        blacksky_spectra_credential="blacksky-secret",
    )
    assert commercial.configuration("planet_commercial", ready)["configured"] is True
    assert commercial.configuration("airbus_oneatlas", ready)["configured"] is True
    assert commercial.configuration("blacksky_spectra", ready)["configured"] is False
    safe_status = repr(commercial.all_statuses(ready))
    for secret in ("planet-secret", "airbus-secret", "blacksky-secret", "contract-ticket-1"):
        assert secret not in safe_status
    assert commercial.status("blacksky_spectra", ready)["connector_available"] is False


def test_commercial_scene_exposes_only_opaque_id_and_proxy_asset(monkeypatch):
    provider_key = "planet_commercial"
    item_url = "https://catalog.example/stac/items/scene-42"
    meta = imagery.get_imagery_provider_meta(provider_key)
    feature = {
        "type": "Feature",
        "id": "scene-42",
        "bbox": [100, -2, 101, -1],
        "properties": {"datetime": "2025-01-02T03:04:05Z", "eo:cloud_cover": 8.1, "platform": "SkySat"},
        "links": [{"rel": "self", "href": item_url}],
        "assets": {
            "analytic": {
                "href": "https://signed-assets.example/image.tif?signature=do-not-expose",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
            }
        },
    }
    monkeypatch.setattr(
        imagery,
        "commercial_configuration",
        lambda _key: {"catalog_url": "https://catalog.example/stac", "configured": True},
    )
    monkeypatch.setattr(imagery, "_validate_public_http_url", lambda *_args, **_kwargs: None)
    scene = imagery._scene_from_stac_item(
        item_url,
        feature,
        meta,
        "Planet",
        SimpleNamespace(base_url="https://savegeo.example/api/"),
    )

    assert scene is not None
    assert scene["id"].startswith("commercial:planet_commercial:")
    assert "signed-assets.example" not in repr(scene)
    assert "signature=do-not-expose" not in repr(scene)
    assert "/imagery/commercial-source?" in scene["assets"][0]["href"]
    assert imagery._resolve_commercial_item(provider_key, scene["id"]) == item_url
    with pytest.raises(AnalysisError, match="tidak valid"):
        imagery._resolve_commercial_item(provider_key, "https://attacker.example/item")


def test_blacksky_is_visible_but_search_and_preview_are_blocked():
    with pytest.raises(AnalysisError) as error:
        imagery.list_scenes({"satellite": "blacksky_spectra"})
    assert error.value.status_code == 503
    assert "dokumentasi/API resmi pelanggan" in str(error.value)
    with pytest.raises(AnalysisError) as tile_error:
        imagery.get_scene_tile({"satellite": "blacksky_spectra", "scene_id": "anything"})
    assert tile_error.value.status_code == 503
