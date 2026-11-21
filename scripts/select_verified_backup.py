"""Select the newest pre-deploy database dump with a valid manifest and hash."""

from __future__ import annotations

import hashlib
import hmac
import re
import sys
from pathlib import Path

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _read_manifest(path: Path) -> dict[str, str] | None:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            return None
        values[key] = value
    return values


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def select_latest_verified_backup(directory: Path) -> Path | None:
    """Return the newest dump whose sidecar manifest identifies and hashes it."""
    if directory.is_symlink() or not directory.is_dir():
        return None

    manifests = sorted(directory.glob("predeploy_*.manifest"), reverse=True)
    for manifest_path in manifests:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        dump_path = manifest_path.with_suffix(".dump")
        if dump_path.is_symlink() or not dump_path.is_file():
            continue

        manifest = _read_manifest(manifest_path)
        if manifest is None or manifest.get("database_dump") != dump_path.name:
            continue
        expected_hash = manifest.get("database_sha256", "")
        if not _SHA256_PATTERN.fullmatch(expected_hash):
            continue
        actual_hash = _sha256(dump_path)
        if actual_hash is not None and hmac.compare_digest(actual_hash, expected_hash):
            return dump_path
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: select_verified_backup.py BACKUP_DIRECTORY", file=sys.stderr)
        return 2
    selected = select_latest_verified_backup(Path(sys.argv[1]))
    if selected is None:
        print("No verified pre-deploy database dump is available.", file=sys.stderr)
        return 4
    print(selected.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
