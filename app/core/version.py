"""Public, non-secret runtime version and deployment metadata."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_PUBLIC_KEYS = {
    "build_number",
    "run_attempt",
    "commit_sha",
    "ref",
    "build_time_utc",
    "deployed_at_utc",
    "workflow_name",
    "workflow_run_id",
    "workflow_run_url",
    "deployment_environment",
    "metadata_source",
}


def get_product_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise RuntimeError("SAVEGEO backend version must be SemVer")
    return version


def _text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _integer(value: object) -> int | None:
    normalized = _text(value)
    if not normalized or not normalized.isdigit():
        return None
    parsed = int(normalized)
    return parsed if parsed > 0 else None


def _deployment_file() -> Path:
    configured = _text(os.getenv("SAVEGEO_DEPLOYMENT_METADATA_FILE"))
    return Path(configured) if configured else ROOT / "var" / "deployment-metadata.json"


def _file_metadata() -> dict[str, object]:
    try:
        raw = json.loads(_deployment_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return {key: raw[key] for key in _PUBLIC_KEYS if key in raw}


def get_public_version_metadata() -> dict[str, object]:
    run_id = _integer(os.getenv("GITHUB_RUN_ID"))
    repository = _text(os.getenv("GITHUB_REPOSITORY"))
    server_url = _text(os.getenv("GITHUB_SERVER_URL")) or "https://github.com"
    metadata: dict[str, object] = {
        "schema_version": 1,
        "component": "backend",
        "product_version": get_product_version(),
        "component_version": get_product_version(),
        "build_number": _integer(os.getenv("SAVEGEO_BUILD_NUMBER") or os.getenv("GITHUB_RUN_NUMBER")),
        "run_attempt": _integer(os.getenv("SAVEGEO_RUN_ATTEMPT") or os.getenv("GITHUB_RUN_ATTEMPT")),
        "commit_sha": _text(os.getenv("SAVEGEO_COMMIT_SHA") or os.getenv("GITHUB_SHA")),
        "ref": _text(os.getenv("SAVEGEO_REF") or os.getenv("GITHUB_REF")),
        "build_time_utc": _text(os.getenv("SAVEGEO_BUILD_TIME_UTC")),
        "deployed_at_utc": _text(os.getenv("SAVEGEO_DEPLOYED_AT_UTC")),
        "workflow_name": _text(os.getenv("SAVEGEO_WORKFLOW_NAME") or os.getenv("GITHUB_WORKFLOW")),
        "workflow_run_id": _integer(os.getenv("SAVEGEO_WORKFLOW_RUN_ID") or os.getenv("GITHUB_RUN_ID")),
        "workflow_run_url": _text(os.getenv("SAVEGEO_WORKFLOW_RUN_URL")) or (f"{server_url}/{repository}/actions/runs/{run_id}" if run_id and repository else None),
        "deployment_environment": _text(os.getenv("SAVEGEO_DEPLOYMENT_ENVIRONMENT")),
        "metadata_source": "workflow" if os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("SAVEGEO_COMMIT_SHA") else "local",
    }
    metadata.update(_file_metadata())
    return metadata
