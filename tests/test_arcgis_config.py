from app.core.default_configs import DEFAULT_CONFIGS, is_sensitive_config_key
from app.providers.arcgis_client import ArcGISClient


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

    assert defaults["arcgis.api_key"] == ""
    assert is_sensitive_config_key("arcgis.api_key")
