from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import gee_service


def test_initialize_ee_prefers_active_credential_project(monkeypatch):
    credential = SimpleNamespace(
        id=7,
        project_id="credential-project",
        client_email="service@example.test",
        bucket_path="gee/key.json",
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = credential
    monkeypatch.setattr(gee_service, "get_settings", lambda: SimpleNamespace(
        gee_project_id="wrong-global-project",
        gee_key_file="",
        gee_service_account="",
        gee_allow_legacy_plaintext_file_fallback=False,
    ))
    monkeypatch.setattr(
        "app.services.storage_service.download_credential",
        lambda path: b'{"type":"service_account"}',
    )
    initialized = {}

    def fake_initialize(email, key_bytes, *, project_id=None):
        initialized.update(email=email, key_bytes=key_bytes, project_id=project_id)
        return True

    monkeypatch.setattr(gee_service, "_init_from_key_data", fake_initialize)

    assert gee_service.initialize_ee(db) is True
    assert initialized["project_id"] == "credential-project"
    assert initialized["email"] == "service@example.test"
    db.commit.assert_called_once()
