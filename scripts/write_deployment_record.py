"""Add a successful health-checked deployment timestamp to build metadata."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--environment", required=True)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    metadata["deployment_environment"] = args.environment
    metadata["deployed_at_utc"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    metadata["deployment_status"] = "succeeded"
    metadata["health_check"] = "passed"
    args.output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote backend deployment record to {args.output}")


if __name__ == "__main__":
    main()
