"""Validate the cross-repository manifest, including the bootstrap/null case."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _timestamp(value: object, field: str) -> None:
    if value is not None:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def validate_manifest(path: Path) -> dict[str, object]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = ("manifest_version", "product_version", "status", "validation_status", "frontend", "backend", "compatibility")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"Missing manifest fields: {', '.join(missing)}")
    if manifest["manifest_version"] != 1 or not SEMVER.fullmatch(str(manifest["product_version"])):
        raise ValueError("Unsupported manifest version or product SemVer")
    for name in ("frontend", "backend"):
        component = manifest[name]
        for field in ("version", "build_number", "run_attempt", "commit_sha", "workflow_name", "workflow_run_id", "workflow_run_url", "environment", "built_at_utc", "deployed_at_utc", "observed_local_commit_sha"):
            if field not in component:
                raise ValueError(f"Missing {name}.{field}")
        if not SEMVER.fullmatch(str(component["version"])):
            raise ValueError(f"Invalid {name}.version")
        if component["build_number"] is not None and (not isinstance(component["build_number"], int) or component["build_number"] < 1):
            raise ValueError(f"Invalid {name}.build_number")
        if component["run_attempt"] is not None and (not isinstance(component["run_attempt"], int) or component["run_attempt"] < 1):
            raise ValueError(f"Invalid {name}.run_attempt")
        for field in ("commit_sha", "observed_local_commit_sha"):
            if component[field] is not None and not SHA.fullmatch(str(component[field])):
                raise ValueError(f"Invalid {name}.{field}")
        _timestamp(component["built_at_utc"], f"{name}.built_at_utc")
        _timestamp(component["deployed_at_utc"], f"{name}.deployed_at_utc")
    if manifest["compatibility"].get("status") not in {"not_verified", "compatible", "different", "metadata_unavailable"}:
        raise ValueError("Invalid compatibility status")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    manifest = validate_manifest(args.path)
    print(f"Validated SAVEGEO manifest: v{manifest['product_version']} ({manifest['validation_status']})")


if __name__ == "__main__":
    main()
