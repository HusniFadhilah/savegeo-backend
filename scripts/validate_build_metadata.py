"""Validate the public build metadata contract without third-party packages."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

FORBIDDEN = re.compile(r"secret|token|password|credential|private[_-]?key|api[_-]?key", re.IGNORECASE)


def validate(path: Path, expected_sha: str | None = None, expected_environment: str | None = None, require_workflow: bool = False) -> dict[str, object]:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    required = ("schema_version", "component", "product_version", "component_version", "build_time_utc")
    missing = [key for key in required if metadata.get(key) in (None, "")]
    if missing:
        raise ValueError(f"Missing metadata fields: {', '.join(missing)}")
    if metadata["schema_version"] != 1 or metadata["component"] != "backend":
        raise ValueError("Unsupported backend metadata schema/component")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(metadata["product_version"])) or metadata["component_version"] != metadata["product_version"]:
        raise ValueError("Invalid or inconsistent backend SemVer")
    commit = metadata.get("commit_sha")
    if commit is not None and not re.fullmatch(r"[0-9a-f]{40}", str(commit), re.IGNORECASE):
        raise ValueError("commit_sha must be a full 40-character SHA or null")
    if expected_sha and commit != expected_sha:
        raise ValueError(f"Metadata commit {commit} does not match expected SHA {expected_sha}")
    if os.getenv("GITHUB_SHA") and commit != os.getenv("GITHUB_SHA"):
        raise ValueError("Metadata commit does not match GITHUB_SHA")
    if expected_environment and metadata.get("deployment_environment") != expected_environment:
        raise ValueError("Metadata deployment environment does not match expected environment")
    if require_workflow and (metadata.get("build_number") is None or metadata.get("run_attempt") is None or not commit or not metadata.get("workflow_run_id")):
        raise ValueError("Workflow metadata is incomplete")
    if any(FORBIDDEN.search(str(key)) for key in metadata):
        raise ValueError("Metadata contains a forbidden secret-like field name")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--expected-sha")
    parser.add_argument("--expected-environment")
    parser.add_argument("--require-workflow", action="store_true")
    args = parser.parse_args()
    metadata = validate(args.path, args.expected_sha, args.expected_environment, args.require_workflow)
    print(f"Validated SAVEGEO backend metadata: {metadata['product_version']}, build {metadata.get('build_number') or 'local'}")


if __name__ == "__main__":
    main()
