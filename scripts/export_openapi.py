"""Export the FastAPI contract for review and release evidence.

Usage from ``backend``: ``python scripts/export_openapi.py docs/compliance/evidence/openapi.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "../docs/compliance/evidence/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
