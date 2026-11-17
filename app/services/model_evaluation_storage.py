"""Encrypted retention for small model holdout datasets used in review."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.core import secret_encryption


def _root() -> Path:
    configured = get_settings().upload_path / "model-evaluations"
    configured.mkdir(parents=True, exist_ok=True)
    if configured.is_symlink():
        raise ValueError("Model evaluation storage path is unsafe")
    return configured.resolve()


def store(dataset_id: int, dataset_sha256: str, raw: bytes) -> str:
    if dataset_id <= 0 or len(dataset_sha256) != 64 or not raw:
        raise ValueError("Invalid model evaluation storage identity")
    root = _root()
    relative = Path(f"{dataset_id}-{dataset_sha256}.enc")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target.is_symlink():
        raise ValueError("Model evaluation storage path is unsafe")
    encrypted = secret_encryption.encrypt(raw, purpose=f"model-evaluation:{dataset_id}:{dataset_sha256}")
    fd, temporary = tempfile.mkstemp(prefix=".evaluation-", dir=root)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return relative.as_posix()


def delete(relative_path: str | None) -> None:
    if not relative_path:
        return
    root = _root()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root) or candidate.is_symlink():
        raise ValueError("Model evaluation storage path is unsafe")
    candidate.unlink(missing_ok=True)


def verify(dataset_id: int, dataset_sha256: str, relative_path: str | None) -> bool:
    if not relative_path:
        return False
    root = _root()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root) or candidate.is_symlink() or not candidate.is_file():
        return False
    encrypted = candidate.read_bytes()
    plaintext = secret_encryption.decrypt(
        encrypted, purpose=f"model-evaluation:{dataset_id}:{dataset_sha256}"
    )
    import hashlib
    return hashlib.sha256(plaintext).hexdigest() == dataset_sha256


def exists(relative_path: str | None) -> bool:
    if not relative_path:
        return False
    root = _root()
    candidate = (root / relative_path).resolve()
    return candidate.is_relative_to(root) and not candidate.is_symlink() and candidate.is_file()
