import base64
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.core import secret_encryption as crypto
from app.services import storage_service as storage


@pytest.fixture
def settings(tmp_path, monkeypatch):
    config = SimpleNamespace(secret_encryption_keys=SecretStr(json.dumps({"v1": base64.b64encode(b"a" * 32).decode()})),
                             secret_encryption_active_key_id="v1", credential_allow_legacy_plaintext_read=True,
                             supabase_url="", supabase_service_role_key="", supabase_gee_credentials_bucket="test-private",
                             upload_path=tmp_path)
    monkeypatch.setattr(crypto, "get_settings", lambda: config)
    monkeypatch.setattr(storage, "get_settings", lambda: config)
    return config


def test_ciphertext_is_bound_to_path_and_key_version(settings):
    data = b'{"private_key":"fixture-only"}'
    encrypted = crypto.encrypt(data, purpose="gee-credential:a.json")
    assert data not in encrypted
    assert crypto.decrypt(encrypted, purpose="gee-credential:a.json") == data
    with pytest.raises(crypto.SecretEncryptionError):
        crypto.decrypt(encrypted, purpose="gee-credential:b.json")
    tampered = bytearray(encrypted)
    tampered[-8] ^= 1
    with pytest.raises(crypto.SecretEncryptionError):
        crypto.decrypt(bytes(tampered), purpose="gee-credential:a.json")
    settings.secret_encryption_keys = SecretStr(json.dumps({"v2": base64.b64encode(b"b" * 32).decode()}))
    settings.secret_encryption_active_key_id = "v2"
    with pytest.raises(crypto.SecretEncryptionError):
        crypto.decrypt(encrypted, purpose="gee-credential:a.json")


def test_local_upload_encrypted_and_keyring_survives_restart(settings):
    data = b'{"private_key":"fixture-only"}'
    storage.upload_credential("one.json", data)
    raw = storage._safe_local_path("one.json").read_bytes()
    assert raw.startswith(crypto.MAGIC) and b"fixture-only" not in raw
    assert storage.download_credential("one.json") == data
    settings.secret_encryption_keys = SecretStr(settings.secret_encryption_keys.get_secret_value())
    assert storage.download_credential("one.json") == data
    settings.secret_encryption_keys = SecretStr("")
    with pytest.raises(storage.StorageNotConfigured):
        storage.upload_credential("two.json", data)
    assert not storage._safe_local_path("two.json").exists()


def test_legacy_rotation_dry_run_backup_and_restore(settings):
    data = b'{"private_key":"fixture-only"}'
    path = storage._safe_local_path("old.json")
    path.write_bytes(data)
    assert storage.rotate_credential("old.json") == {"was_encrypted": False, "applied": False}
    assert path.read_bytes() == data
    result = storage.rotate_credential("old.json", apply=True)
    assert result["applied"]
    assert storage.download_credential(result["backup_path"]) == data
    assert b"fixture-only" not in path.read_bytes()
    assert b"fixture-only" not in storage._read_object(result["backup_path"])
    settings.credential_allow_legacy_plaintext_read = False
    assert storage.download_credential("old.json") == data
    path.write_bytes(data)
    with pytest.raises(crypto.SecretEncryptionError):
        storage.download_credential("old.json")


@pytest.mark.parametrize("path", ["../outside", "x/../../outside", "C:/secret", "a\\..\\secret", "/absolute", ""])
def test_local_storage_rejects_escaping_paths(settings, path):
    with pytest.raises(ValueError):
        storage._safe_local_path(path)


def test_rotation_retains_decryptable_old_key(settings):
    storage.upload_credential("key.json", b"fixture")
    old = storage._read_object("key.json")
    settings.secret_encryption_keys = SecretStr(json.dumps({"v1": base64.b64encode(b"a" * 32).decode(), "v2": base64.b64encode(b"b" * 32).decode()}))
    settings.secret_encryption_active_key_id = "v2"
    assert storage.download_credential("key.json") == b"fixture"
    storage.rotate_credential("key.json", apply=True)
    assert storage._read_object("key.json") != old
    settings.secret_encryption_keys = SecretStr(json.dumps({"v2": base64.b64encode(b"b" * 32).decode()}))
    assert storage.download_credential("key.json") == b"fixture"
