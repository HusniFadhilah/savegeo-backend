"""Write non-secret build/deployment metadata for SAVEGEO."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml does not contain a project version")
    return match.group(1)


def text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def integer(value: object) -> int | None:
    normalized = text(value)
    if not normalized or not normalized.isdigit():
        return None
    parsed = int(normalized)
    return parsed if parsed > 0 else None


def build_metadata() -> dict[str, object]:
    run_id = integer(os.getenv("GITHUB_RUN_ID"))
    repository = text(os.getenv("GITHUB_REPOSITORY"))
    server_url = text(os.getenv("GITHUB_SERVER_URL")) or "https://github.com"
    return {
        "schema_version": 1,
        "component": "backend",
        "product_version": project_version(),
        "component_version": project_version(),
        "build_number": integer(os.getenv("GITHUB_RUN_NUMBER")),
        "run_attempt": integer(os.getenv("GITHUB_RUN_ATTEMPT")),
        "commit_sha": text(os.getenv("GITHUB_SHA")),
        "ref": text(os.getenv("GITHUB_REF")),
        "build_time_utc": text(os.getenv("SAVEGEO_BUILD_TIME_UTC")) or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deployed_at_utc": text(os.getenv("SAVEGEO_DEPLOYED_AT_UTC")),
        "workflow_name": text(os.getenv("GITHUB_WORKFLOW")),
        "workflow_run_id": run_id,
        "workflow_run_url": f"{server_url}/{repository}/actions/runs/{run_id}" if run_id and repository else None,
        "deployment_environment": text(os.getenv("SAVEGEO_DEPLOYMENT_ENVIRONMENT")),
        "metadata_source": "github-actions" if os.getenv("GITHUB_ACTIONS") == "true" else "local",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--deployment-time-utc")
    parser.add_argument("--deployment-environment")
    args = parser.parse_args()
    metadata = build_metadata()
    if args.deployment_time_utc:
        metadata["deployed_at_utc"] = args.deployment_time_utc
    if args.deployment_environment:
        metadata["deployment_environment"] = args.deployment_environment
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote SAVEGEO backend metadata to {args.output}")


if __name__ == "__main__":
    main()
