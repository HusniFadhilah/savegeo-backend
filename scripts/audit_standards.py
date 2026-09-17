"""Static inventory for temporal, GeoJSON, TileJSON, and vertical metadata.

The audit is deliberately conservative: it reports candidates for human review
and gates only unambiguous anti-patterns. It does not rewrite source or infer a
timezone/datum for legacy data.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md"}
SKIP_PARTS = {".venv", "node_modules", "dist", "__pycache__", ".git"}

PATTERNS = {
    "naive_datetime_parser": re.compile(r"datetime\.utcnow\("),
    "datetime_fromisoformat_review": re.compile(r"datetime\.fromisoformat\([^\n]*\)"),
    "browser_date_constructor": re.compile(r"new Date\([^)]*\)"),
    "legacy_geojson_crs": re.compile(r"[\"']crs[\"']\s*:"),
    "vertical_egm96": re.compile(r"\bEGM96\b", re.IGNORECASE),
    "ambiguous_temporal_field": re.compile(r"\b(?:date|time|timestamp|elevation|altitude|height)\b"),
}


def files() -> list[Path]:
    roots = [ROOT / "app", ROOT / "tests", ROOT.parent / "frontend" / "src"]
    return [
        path
        for base in roots
        for path in base.rglob("*")
        if path.is_file() and path.suffix in TEXT_SUFFIXES and not (SKIP_PARTS & set(path.parts))
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail only on unambiguous forbidden patterns")
    args = parser.parse_args()
    inventory = {key: [] for key in PATTERNS}
    scanned = 0
    for path in files():
        scanned += 1
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in PATTERNS.items():
            for line_number, line in enumerate(content.splitlines(), 1):
                if pattern.search(line):
                    inventory[name].append({"file": str(path.relative_to(ROOT.parent)).replace("\\", "/"), "line": line_number})
    summary = {
        "files_scanned": scanned,
        "inventory": {key: len(value) for key, value in inventory.items()},
        "findings": inventory,
        "policy": {
            "timestamp": "RFC3339 with timezone for instants; YYYY-MM-DD for calendar dates",
            "geojson": "RFC 7946 / EPSG:4326 longitude,latitude",
            "tilejson": "3.0.0",
            "vertical_reference": "EGM2008 only when source and grid/control value are known",
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.check:
        forbidden = [
            item
            for item in inventory["naive_datetime_parser"]
            if item["file"].startswith(("backend/app/", "frontend/src/"))
        ]
        if forbidden:
            print("Standards audit failed: forbidden temporal/datum pattern detected.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
