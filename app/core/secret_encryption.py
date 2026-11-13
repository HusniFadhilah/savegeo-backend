"""Authenticated encryption with an operator-managed keyring outside storage/DB.

AES-256-GCM binds each envelope to its purpose/object path and key version.
No key is generated implicitly and no plaintext fallback exists for writes.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

MAGIC = b"SAVEGEO-SECRET-V1\n"


class SecretEncryptionError(RuntimeError):
    """Safe, non-sensitive error that may be shown to an operator."""


def _keyring() -> tuple[str, dict[str, bytes]]:
    settings = get_settings()
    active = settings.secret_encryption_active_key_id
    try:
        raw = json.loads(settings.secret_encryption_keys.get_secret_value())
        if not isinstance(raw, dict) or not raw or len(raw) > 20:
            raise ValueError
        keys = {}
        for name, value in raw.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 64 or not isinstance(value, str):
                raise ValueError
            key = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
            if len(key) != 32:
                raise ValueError
            keys[name] = key
        if active not in keys:
            raise ValueError
        return active, keys
    except (ValueError, TypeError, UnicodeError):
        raise SecretEncryptionError("Konfigurasi kunci enkripsi server belum tersedia atau tidak valid.") from None


def _aad(key_id: str, purpose: str) -> bytes:
    return json.dumps(["savegeo-secret-v1", key_id, purpose], separators=(",", ":")).encode()


def encrypt(data: bytes, *, purpose: str) -> bytes:
    active, keys = _keyring()
    nonce = os.urandom(12)
    ciphertext = AESGCM(keys[active]).encrypt(nonce, data, _aad(active, purpose))
    return MAGIC + json.dumps({"key_id": active, "nonce": base64.b64encode(nonce).decode(),
                                "ciphertext": base64.b64encode(ciphertext).decode()}, separators=(",", ":")).encode()


def decrypt(data: bytes, *, purpose: str) -> bytes:
    _, keys = _keyring()
    try:
        if not data.startswith(MAGIC):
            raise ValueError
        envelope = json.loads(data[len(MAGIC):])
        key_id = envelope["key_id"]
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        if len(nonce) != 12:
            raise ValueError
        return AESGCM(keys[key_id]).decrypt(nonce, ciphertext, _aad(key_id, purpose))
    except (InvalidTag, ValueError, TypeError, KeyError, UnicodeError):
        raise SecretEncryptionError("Kredensial terenkripsi tidak dapat dibuka. Periksa keyring atau pulihkan cadangan yang sah.") from None
