from sqlalchemy import create_engine, text

from scripts.db_migration_inventory import capture_inventory, compare_inventories


def test_inventory_records_exact_table_counts_without_row_contents():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"))
        connection.execute(text("INSERT INTO users (id, email) VALUES (1, 'private@example.test'), (2, 'other@example.test')"))

    result = capture_inventory(engine)

    assert result["tables"] == {"users": 2}
    assert result["total_rows"] == 2
    assert "private@example.test" not in str(result)
    assert len(result["user_identity_hashes"]["users"]) == 2
    engine.dispose()


def test_account_identity_check_detects_deleted_accounts_but_allows_new_accounts():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO users (id) VALUES (1), (2)"))
    before = capture_inventory(engine, identity_salt=b"fixed-private-test-salt")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id) VALUES (3)"))
    after = capture_inventory(engine, identity_salt=b"fixed-private-test-salt")
    assert compare_inventories(before, after)["status"] == "passed"

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id=2"))
    deleted = capture_inventory(engine, identity_salt=b"fixed-private-test-salt")
    result = compare_inventories(before, deleted)
    assert result["status"] == "failed"
    assert result["removed_user_or_admin_identities"] == {"users": 1}
    engine.dispose()


def test_comparison_allows_additions_but_blocks_missing_tables_or_rows():
    before = {
        "schema": "savegeo-table-row-counts/v1",
        "tables": {"users": 2, "plots": 4},
        "counts_sha256": "before",
        "alembic_revisions": ["0062"],
        "user_identity_hashes": {},
    }
    safe_after = {
        "schema": "savegeo-table-row-counts/v1",
        "tables": {"users": 2, "plots": 5, "catalog": 0},
        "counts_sha256": "after",
        "alembic_revisions": ["0063"],
        "user_identity_hashes": {},
    }
    assert compare_inventories(before, safe_after)["status"] == "passed"

    unsafe_after = {**safe_after, "tables": {"users": 1}}
    result = compare_inventories(before, unsafe_after)
    assert result["status"] == "failed"
    assert result["removed_tables"] == ["plots"]
    assert result["tables_with_decreased_rows"] == {"users": {"before": 2, "after": 1}}
