"""Capture and compare exact public-table row counts around production migrations.

The inventory intentionally contains table names and row counts only. It never
copies record contents, credentials, or user identifiers into deployment
evidence.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text

VOLATILE_ROW_COUNT_TABLES = frozenset({"request_rate_buckets"})


def capture_inventory(engine: Engine, *, identity_salt: bytes | None = None) -> dict[str, Any]:
    inspector = inspect(engine)
    schema = "public" if engine.dialect.name == "postgresql" else None
    preparer = engine.dialect.identifier_preparer
    table_names = sorted(inspector.get_table_names(schema=schema))
    tables: dict[str, int] = {}
    user_identity_hashes: dict[str, list[str]] = {}
    salt = identity_salt or secrets.token_bytes(32)
    with engine.connect() as connection:
        for table_name in table_names:
            qualified = (
                f"{preparer.quote_schema(schema)}.{preparer.quote(table_name)}"
                if schema
                else preparer.quote(table_name)
            )
            count = connection.execute(text(f"SELECT count(*) FROM {qualified}")).scalar_one()
            tables[table_name] = int(count)
            if table_name in {"users", "admin_users"}:
                primary_key = inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") or []
                if primary_key:
                    columns = ", ".join(preparer.quote(column) for column in primary_key)
                    identity_query = text(f"SELECT {columns} FROM {qualified} ORDER BY {columns}")
                    hashes = set()
                    for row in connection.execution_options(stream_results=True).execute(identity_query):
                        value = json.dumps(list(row), sort_keys=True, separators=(",", ":"), default=str)
                        hashes.add(hashlib.sha256(salt + value.encode("utf-8")).hexdigest())
                    user_identity_hashes[table_name] = sorted(hashes)
        revision = None
        if "alembic_version" in tables:
            revision = connection.execute(
                text(f"SELECT version_num FROM {preparer.quote('alembic_version')} ORDER BY version_num")
            ).scalars().all()

    fingerprint = hashlib.sha256(
        json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "savegeo-table-row-counts/v1",
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "database_dialect": engine.dialect.name,
        "alembic_revisions": revision,
        "table_count": len(tables),
        "total_rows": sum(tables.values()),
        "tables": tables,
        "counts_sha256": fingerprint,
        # Salted one-way keys let the comparison detect removed user/admin
        # accounts without placing their identifiers in deployment evidence.
        "user_identity_salt_b64": base64.b64encode(salt).decode("ascii"),
        "user_identity_hashes": user_identity_hashes,
    }


def compare_inventories(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("schema") != "savegeo-table-row-counts/v1" or after.get("schema") != before.get("schema"):
        raise ValueError("Unsupported migration inventory schema")
    old_tables = before.get("tables")
    new_tables = after.get("tables")
    if not isinstance(old_tables, dict) or not isinstance(new_tables, dict):
        raise ValueError("Migration inventory does not contain a table-count map")

    removed = sorted(set(old_tables) - set(new_tables))
    volatile_count_changes = {
        name: {"before": int(old_tables[name]), "after": int(new_tables[name])}
        for name in sorted(VOLATILE_ROW_COUNT_TABLES & set(old_tables) & set(new_tables))
        if int(new_tables[name]) != int(old_tables[name])
    }
    decreased = {
        name: {"before": int(old_tables[name]), "after": int(new_tables[name])}
        for name in sorted(set(old_tables) & set(new_tables))
        if name not in VOLATILE_ROW_COUNT_TABLES and int(new_tables[name]) < int(old_tables[name])
    }
    added = sorted(set(new_tables) - set(old_tables))
    before_identities = before.get("user_identity_hashes") or {}
    after_identities = after.get("user_identity_hashes") or {}
    removed_user_identities = {
        table: len(set(keys) - set(after_identities.get(table, [])))
        for table, keys in before_identities.items()
        if set(keys) - set(after_identities.get(table, []))
    }
    result = {
        "schema": "savegeo-migration-preservation-check/v1",
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "before_counts_sha256": before.get("counts_sha256"),
        "after_counts_sha256": after.get("counts_sha256"),
        "before_alembic_revisions": before.get("alembic_revisions"),
        "after_alembic_revisions": after.get("alembic_revisions"),
        "tables_before": len(old_tables),
        "tables_after": len(new_tables),
        "rows_before": sum(int(value) for value in old_tables.values()),
        "rows_after": sum(int(value) for value in new_tables.values()),
        "added_tables": added,
        "removed_tables": removed,
        "tables_with_decreased_rows": decreased,
        "volatile_table_count_changes": volatile_count_changes,
        "removed_user_or_admin_identities": removed_user_identities,
        "status": "passed" if not removed and not decreased and not removed_user_identities else "failed",
    }
    return result


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".partial", delete=False,
        ) as output:
            temporary_name = output.name
            json.dump(payload, output, sort_keys=True, indent=2)
            output.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture", help="Capture exact public-table row counts")
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--salt-source", type=Path, help="Reuse the private identity salt from a prior inventory")
    compare = subparsers.add_parser("compare", help="Fail if a table or rows disappeared")
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "capture":
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            parser.error("DATABASE_URL is required")
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            identity_salt = None
            if args.salt_source:
                prior = json.loads(args.salt_source.read_text(encoding="utf-8"))
                identity_salt = base64.b64decode(prior["user_identity_salt_b64"], validate=True)
            inventory = capture_inventory(engine, identity_salt=identity_salt)
        finally:
            engine.dispose()
        _write_private_json(args.output, inventory)
        print(
            "Private database inventory captured; "
            f"Alembic revisions={inventory['alembic_revisions']}"
        )
        return 0

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    result = compare_inventories(before, after)
    _write_private_json(args.output, result)
    print(
        f"Migration preservation check {result['status']}; "
        f"Alembic={result['before_alembic_revisions']} -> {result['after_alembic_revisions']}; "
        f"evidence_sha256={hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()}"
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
