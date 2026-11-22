import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import credential_probe as service


@pytest.mark.parametrize("status,body,expected", [(200, {"result": 1}, "ok"), (403, {"private": "never expose"}, "permission_denied"), (429, {}, "rate_limited"), (500, {}, "provider_error")])
def test_probe_uses_isolated_authorized_session(monkeypatch, status, body, expected):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(gee_project_id="test-project"))
    monkeypatch.setattr(service.service_account.Credentials, "from_service_account_info", lambda *args, **kwargs: object())
    session = MagicMock()
    session.__enter__.return_value.post.return_value = SimpleNamespace(status_code=status, json=lambda: body)
    monkeypatch.setattr(service, "pinned_google_authorized_session", lambda *args, **kwargs: session)
    raw = json.dumps({"type": "service_account", "project_id": "test-project", "client_email": "test@example.test", "token_uri": "https://oauth2.googleapis.com/token"}).encode()
    result = service.probe(raw)
    assert result.code == expected and result.ok == (expected == "ok")
    assert "never expose" not in json.dumps(result.to_dict())
    call = session.__enter__.return_value.post.call_args
    assert call.args[0] == "https://earthengine.googleapis.com/v1/projects/test-project/value:compute"
    assert call.kwargs["json"] == {"expression": {
        "values": {"probe_constant": {"constantValue": 1}},
        "result": "probe_constant",
    }}


def test_probe_rejects_custom_token_endpoint_before_network(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(gee_project_id="test-project"))
    def unexpected(*args, **kwargs):
        pytest.fail("Untrusted token endpoint must not be contacted")
    monkeypatch.setattr(service, "pinned_google_authorized_session", unexpected)
    assert service.probe(json.dumps({"type": "service_account", "project_id": "test-project", "token_uri": "http://127.0.0.1/private"}).encode()).code == "invalid_key_metadata"


def test_probe_reports_google_transport_errors_as_connectivity_failures(monkeypatch):
    from google.auth.exceptions import TransportError

    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(gee_project_id="test-project"))
    monkeypatch.setattr(service.service_account.Credentials, "from_service_account_info", lambda *args, **kwargs: object())
    session = MagicMock()
    session.__enter__.return_value.post.side_effect = TransportError("network unavailable")
    monkeypatch.setattr(service, "pinned_google_authorized_session", lambda *args, **kwargs: session)

    raw = json.dumps({
        "type": "service_account",
        "project_id": "test-project",
        "client_email": "test@example.test",
        "token_uri": "https://oauth2.googleapis.com/token",
    }).encode()
    result = service.probe(raw)

    assert result.code == "connection_failed"
    assert result.ok is False
    assert "network unavailable" not in json.dumps(result.to_dict())


def test_probe_prefers_the_project_embedded_in_the_key(monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(gee_project_id="wrong-global-project"))
    monkeypatch.setattr(service.service_account.Credentials, "from_service_account_info", lambda *args, **kwargs: object())
    session = MagicMock()
    session.__enter__.return_value.post.return_value = SimpleNamespace(status_code=200, json=lambda: {"result": 1})
    monkeypatch.setattr(service, "pinned_google_authorized_session", lambda *args, **kwargs: session)

    raw = json.dumps({
        "type": "service_account",
        "project_id": "credential-project",
        "client_email": "test@example.test",
        "token_uri": "https://oauth2.googleapis.com/token",
    }).encode()

    result = service.probe(raw)

    assert result.ok is True
    assert result.project_id == "credential-project"
    assert session.__enter__.return_value.post.call_args.args[0] == (
        "https://earthengine.googleapis.com/v1/projects/credential-project/value:compute"
    )
