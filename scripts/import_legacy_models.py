"""One-off import of legacy `backend/saved_models/*.pkl` files into the `uploaded_models`
table. Not run automatically at startup (unlike the old Flask app) — run manually when
migrating an existing deployment's model files.

Usage:
    python -m scripts.import_legacy_models /path/to/old/backend/saved_models
"""
from __future__ import annotations

import sys

from app.db.session import SessionLocal
from app.repositories.uploaded_model_repo import import_legacy_models


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.import_legacy_models <saved_models_dir>", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        result = import_legacy_models(db, sys.argv[1])
        print(
            f"Imported: {result['imported']}, updated: {result.get('updated', 0)}, "
            f"skipped: {result['skipped']}, errors: {len(result['errors'])}"
        )
        for err in result["errors"]:
            print(f"  ERROR {err['file']}: {err['error']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
