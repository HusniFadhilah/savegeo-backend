#!/usr/bin/env python3
"""Extract a backup tar archive into a new, isolated directory safely."""
from __future__ import annotations

import pathlib
import shutil
import sys
import tarfile


def extract(archive_path: pathlib.Path, destination: pathlib.Path) -> int:
    root = destination.resolve(strict=True)
    if destination.is_symlink() or not root.is_dir():
        raise ValueError("Restore destination must be a regular directory")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        normalized: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        for member in members:
            path = pathlib.PurePosixPath(member.name)
            parts = tuple(part for part in path.parts if part not in ("", "."))
            if (path.is_absolute() or not parts or ".." in parts or member.issym() or member.islnk()
                    or not (member.isdir() or member.isfile())):
                raise ValueError("Backup contains an unsafe or unsupported path")
            target = root.joinpath(*parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError("Backup path escapes restore destination")
            normalized.append((member, parts))

        restored = 0
        for member, parts in normalized:
            target = root.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Backup contains an unreadable file")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
            restored += 1
    return restored


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: safe_extract_backup_tar.py ARCHIVE DESTINATION")
    try:
        print(extract(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])))
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise SystemExit(f"Backup extraction validation failed: {exc}") from exc
