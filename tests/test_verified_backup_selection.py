from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.select_verified_backup import select_latest_verified_backup


def _write_backup(directory: Path, stamp: str, content: bytes) -> Path:
    dump = directory / f"predeploy_{stamp}.dump"
    dump.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    (directory / f"predeploy_{stamp}.manifest").write_text(
        f"database_dump={dump.name}\ndatabase_sha256={checksum}\n",
        encoding="utf-8",
    )
    return dump


def test_selects_latest_verified_backup_and_ignores_unmanifested_partial(
    tmp_path: Path,
) -> None:
    older = _write_backup(tmp_path, "20261121T200000Z", b"verified-old")
    (tmp_path / "predeploy_20261121T210000Z.dump").write_bytes(b"partial-new")

    assert select_latest_verified_backup(tmp_path) == older


def test_skips_corrupt_newest_manifest_and_returns_latest_valid_dump(
    tmp_path: Path,
) -> None:
    valid = _write_backup(tmp_path, "20261121T200000Z", b"valid")
    corrupt = _write_backup(tmp_path, "20261121T210000Z", b"changed")
    corrupt.write_bytes(b"tampered after manifest")

    assert select_latest_verified_backup(tmp_path) == valid


def test_rejects_symlinked_backup_files(tmp_path: Path) -> None:
    target = _write_backup(tmp_path, "20261121T200000Z", b"verified")
    manifest = target.with_suffix(".manifest")
    manifest.unlink()
    try:
        manifest.symlink_to(tmp_path / "missing-manifest")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable in this environment: {exc}")

    assert select_latest_verified_backup(tmp_path) is None
