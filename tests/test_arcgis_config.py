from types import SimpleNamespace

from app.core.default_configs import DEFAULT_CONFIGS, is_sensitive_config_key
from app.providers.arcgis_client import ArcGISClient
from app.services import arcgis_service


def test_arcgis_api_key_is_write_only_and_used_by_rest_requests():
    client = ArcGISClient()
    client.configure(
        enabled=True,
        portal_url="https://www.arcgis.com/",
        auth_mode="api_key",
        api_key="arcgis-secret-value",
        timeout=17,
    )
    client._fetch_portal_info = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("upstream URL contained token=arcgis-secret-value")
    )

    status = client.get_status()

    assert client._get_auth_params() == {"token": "arcgis-secret-value"}
    assert status["configured"] is True
    assert status["portal_url"] == "https://www.arcgis.com"
    assert status["request_timeout"] == 17
    assert "arcgis-secret-value" not in str(status)
    assert status["portal_error"] == "Portal check failed (RuntimeError)"


def test_arcgis_api_key_config_is_marked_secret():
    defaults = {key: value for key, value, *_ in DEFAULT_CONFIGS}

    # Keep assertion diagnostics value-free if a developer's environment has
    # an API key configured.
    has_default_key = bool(defaults["arcgis.api_key"])
    assert has_default_key is False
    assert is_sensitive_config_key("arcgis.api_key")


def test_arcgis_worker_refreshes_database_config_after_cache_ttl(monkeypatch):
    clock = [100.0]
    values = {
        "arcgis.enabled": True,
        "arcgis.portal_url": "https://www.arcgis.com",
        "arcgis.auth_mode": "api_key",
        "arcgis.api_key": "first-test-key",
        "arcgis.request_timeout": 17,
    }
    monkeypatch.setattr(arcgis_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(arcgis_service.config_service, "get_all_settings", lambda *args, **kwargs: [])
    monkeypatch.setattr(arcgis_service.config_service, "get_setting", lambda db, key, default: values.get(key, default))
    monkeypatch.setattr(arcgis_service, "get_settings", lambda: SimpleNamespace(
        arcgis_enabled=False,
        arcgis_portal_url="https://www.arcgis.com",
        arcgis_auth_mode="none",
        arcgis_api_key="",
        arcgis_request_timeout=30,
    ))
    client = ArcGISClient()
    monkeypatch.setattr(arcgis_service, "get_arcgis_client", lambda: client)
    monkeypatch.setattr(arcgis_service, "_arcgis_configured_at", 0.0)

    client = arcgis_service.configure_arcgis_from_db(object(), force=True)
    assert client._api_key == "first-test-key"

    values["arcgis.api_key"] = "second-test-key"
    assert arcgis_service.configure_arcgis_from_db(object())._api_key == "first-test-key"

    clock[0] += arcgis_service._ARC_GIS_CONFIG_TTL_SECONDS + 1
    assert arcgis_service.configure_arcgis_from_db(object())._api_key == "second-test-key"
