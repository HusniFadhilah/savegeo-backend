"""python -m app.commands.rotate_credentials [--credential-id N] [--apply]."""
import argparse
import json

from sqlalchemy import select

from app.db.models.gee_credential import GEECredential
from app.db.session import SessionLocal
from app.services import audit_service, storage_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypt/rotate GEE objects. Dry-run unless --apply is supplied.")
    parser.add_argument("--credential-id", type=int, action="append")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        query = select(GEECredential).order_by(GEECredential.id)
        if args.credential_id:
            query = query.where(GEECredential.id.in_(args.credential_id))
        rows = list(db.scalars(query))
        failures = 0
        for row in rows:
            try:
                result = storage_service.rotate_credential(row.bucket_path, apply=args.apply)
                if args.apply:
                    audit_service.log_audit(db, None, "credential.encryption.rotate", "gee_credential", row.id, detail=result)
                print(json.dumps({"credential_id": row.id, **result}))
            except Exception as exc:  # noqa: BLE001 - output never includes object/key material
                failures += 1
                print(json.dumps({"credential_id": row.id, "applied": False, "error_type": type(exc).__name__}))
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
