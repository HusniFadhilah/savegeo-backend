"""Supabase Storage wrapper — used only to hold GEE service-account JSON key files.

Security notes:
  - The bucket configured by `SUPABASE_GEE_CREDENTIALS_BUCKET` MUST be created as
    **private** in the Supabase dashboard (Storage -> New bucket -> Public: OFF).
    This module never generates public URLs; it always uses the service-role key
    to fetch bytes server-side.
  - `SUPABASE_SERVICE_ROLE_KEY` bypasses Row Level Security and can read/write the
    entire project's storage — treat it like a root credential. Store it only in
    the backend's environment, rotate it if ever exposed, never send it to a client.
  - The GEE private key is decrypted-at-rest from Supabase's point of view (Supabase
    Storage encrypts at rest, but the object itself is the plain JSON key). If you
    need stronger guarantees, wrap `upload_bytes` with client-side encryption before
    upload and decrypt in `download_bytes`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import PurePosixPath

from app.core.config import get_settings


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
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError("Invalid storage path")
    base = settings.upload_path / settings.supabase_gee_credentials_bucket
    base.mkdir(parents=True, exist_ok=True)
    return base / normalized.as_posix()


def upload_credential(path: str, data: bytes, content_type: str = "application/json") -> str:
    """Upload a GEE credential JSON file to the private bucket. Returns the bucket-relative path."""
    if _use_local_storage():
        local_path = _safe_local_path(path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return path

    settings = get_settings()
    client = _get_client()
    bucket = client.storage.from_(settings.supabase_gee_credentials_bucket)
    bucket.upload(path, data, {"content-type": content_type, "upsert": "true"})
    return path


def download_credential(path: str) -> bytes:
    if _use_local_storage():
        return _safe_local_path(path).read_bytes()

    settings = get_settings()
    client = _get_client()
    bucket = client.storage.from_(settings.supabase_gee_credentials_bucket)
    return bucket.download(path)


def delete_credential(path: str) -> None:
    if _use_local_storage():
        _safe_local_path(path).unlink(missing_ok=True)
        return

    settings = get_settings()
    client = _get_client()
    bucket = client.storage.from_(settings.supabase_gee_credentials_bucket)
    bucket.remove([path])
