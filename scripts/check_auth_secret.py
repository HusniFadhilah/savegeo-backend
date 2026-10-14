"""Fail deployment if the server signing key occurs in public frontend files.

Run inside the API image with the frontend directory mounted read-only.
Neither the key nor matching file contents are written to output.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.config import get_settings


def scan_frontend(secret: str, directory: Path) -> tuple[int, int]:
    if not directory.is_dir():
        raise ValueError("Frontend directory must exist for the signing-key exposure check")
    if not secret:
        raise ValueError("Signing key must be configured")
    needle = secret.encode("utf-8")
    checked = matches = 0
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".html", ".js", ".mjs", ".css", ".json", ".map"}:
            checked += 1
            matches += int(needle in path.read_bytes())
    if checked == 0:
        raise ValueError("No public frontend files found for the signing-key exposure check")
    return checked, matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-dist", type=Path, required=True)
    args = parser.parse_args()
    checked, matches = scan_frontend(get_settings().jwt_secret_key, args.frontend_dist)
    print(json.dumps({"files_checked": checked, "files_containing_signing_key": matches}))
    if matches:
        parser.exit(1, "Signing key occurs in public frontend files; rotate it before deployment.\n")


if __name__ == "__main__":
    main()
