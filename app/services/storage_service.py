"""Supabase Storage wrapper — used only to hold GEE service-account JSON key files.

Security notes:
  - The bucket configured by `SUPABASE_GEE_CREDENTIALS_BUCKET` MUST be created as
    **private** in the Supabase dashboard (Storage -> New bucket -> Public: OFF).
    This module never generates public URLs; it always uses the service-role key
    to fetch bytes server-side.
  - `SUPABASE_SERVICE_ROLE_KEY` bypasses Row Level Security and can read/write the
    entire project's storage — treat it like a root credential. Store it only in
    the backend's environment, rotate it if ever exposed, never send it to a client.
  - New objects use authenticated application encryption. Encryption keys live in
    the operator keyring, outside this bucket and the database. Legacy plaintext
    reads are a temporary configurable compatibility path, never a write path.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import PurePosixPath
import os
import tempfile

from app.core.config import get_settings
from app.core import secret_encryption


class StorageNotConfigured(RuntimeError):
    pass


@lru_cache
def _get_client():
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise StorageNotConfigured(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — GEE credential upload via "
            "Supabase Storage is unavailable until these are configured."
        )
    from supabase import create_client

    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def _use_local_storage() -> bool:
    settings = get_settings()
    return not settings.supabase_url or not settings.supabase_service_role_key


def _safe_local_path(path: str):
    settings = get_settings()
    normalized = PurePosixPath(path)
    if not path or len(path) > 1024 or "\\" in path or ":" in path or "\x00" in path or normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("Invalid storage path")
    base = settings.upload_path / settings.supabase_gee_credentials_bucket
    base.mkdir(parents=True, exist_ok=True)
    target = (base / normalized.as_posix()).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ValueError("Invalid storage path")
    return target


def upload_credential(path: str, data: bytes, content_type: str = "application/json") -> str:
    """Encrypt before writing to either backend; no plaintext fallback."""
    try:
        encrypted = secret_encryption.encrypt(data, purpose="gee-credential:" + path)
    except secret_encryption.SecretEncryptionError as exc:
        raise StorageNotConfigured(str(exc)) from None
    _write_object(path, encrypted, "application/octet-stream")
    return path


def _write_object(path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    if _use_local_storage():
        local_path = _safe_local_path(path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=".credential-", dir=local_path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(staged, 0o600)
            os.replace(staged, local_path)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)
        return

    settings = get_settings()
    client = _get_client()
    bucket = client.storage.from_(settings.supabase_gee_credentials_bucket)
    bucket.upload(path, data, {"content-type": content_type, "upsert": "true"})


def download_credential(path: str) -> bytes:
    data = _read_object(path)
    if data.startswith(secret_encryption.MAGIC):
        return secret_encryption.decrypt(data, purpose="gee-credential:" + path)
    if not get_settings().credential_allow_legacy_plaintext_read:
        raise secret_encryption.SecretEncryptionError("Kredensial lama perlu dimigrasikan ke penyimpanan terenkripsi.")
    return data


def _read_object(path: str) -> bytes:
    if _use_local_storage():
        return _safe_local_path(path).read_bytes()

    settings = get_settings()
    client = _get_client()
    bucket = client.storage.from_(settings.supabase_gee_credentials_bucket)
    return bucket.download(path)


def rotate_credential(path: str, *, apply: bool = False) -> dict:
    """Explicit operator migration. Back up ciphertext, verify, then replace.

    Returns metadata only. A backup is kept encrypted under the active key,
    bound to its own object path; no plaintext is printed or copied to backup.
    """
    import hashlib
    import uuid
    original = _read_object(path)
    encrypted_before = original.startswith(secret_encryption.MAGIC)
    plaintext = secret_encryption.decrypt(original, purpose="gee-credential:" + path) if encrypted_before else original
    replacement = secret_encryption.encrypt(plaintext, purpose="gee-credential:" + path)
    if secret_encryption.decrypt(replacement, purpose="gee-credential:" + path) != plaintext:
        raise secret_encryption.SecretEncryptionError("Pemeriksaan enkripsi gagal.")
    result = {"was_encrypted": encrypted_before, "applied": False}
    if not apply:
        return result
    backup = "rotation-backups/" + uuid.uuid4().hex + ".enc"
    _write_object(backup, secret_encryption.encrypt(plaintext, purpose="gee-credential:" + backup))
    if secret_encryption.decrypt(_read_object(backup), purpose="gee-credential:" + backup) != plaintext:
        raise secret_encryption.SecretEncryptionError("Pemeriksaan cadangan enkripsi gagal.")
    # Detect intervening replacement before touching the live object. Operators
    # must still pause credential writes for a remote-storage rotation window.
    if hashlib.sha256(_read_object(path)).digest() != hashlib.sha256(original).digest():
        raise secret_encryption.SecretEncryptionError("Kredensial berubah selama rotasi; ulangi pada jendela pemeliharaan.")
    _write_object(path, replacement)
    if download_credential(path) != plaintext:
        raise secret_encryption.SecretEncryptionError("Verifikasi rotasi gagal; cadangan terenkripsi tersedia.")
    return {**result, "applied": True, "backup_path": backup}


def delete_credential(path: str) -> None:
    if _use_local_storage():
        _safe_local_path(path).unlink(missing_ok=True)
        return

    settings = get_settings()
    client = _get_client()
    bucket = client.storage.from_(settings.supabase_gee_credentials_bucket)
    bucket.remove([path])
