from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, create_engine, inspect, select


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0027_viewer_feature_permissions.py"
    spec = importlib.util.spec_from_file_location("viewer_feature_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, migration, direction: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(migration, direction)()


def test_viewer_feature_migration_preserves_preexisting_role_permissions_on_downgrade():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    roles = Table(
        "roles",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(64), nullable=False, unique=True),
        Column("description", Text),
        Column("is_default", Boolean, nullable=False, default=False),
        Column("created_at", DateTime),
    )
    permissions = Table(
        "permissions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(128), nullable=False, unique=True),
        Column("description", Text),
    )
    role_permissions = Table(
        "role_permissions",
        metadata,
        Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        Column("permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    try:
        with engine.begin() as connection:
            connection.execute(roles.insert(), [
                {"id": 1, "name": "admin", "is_default": True},
                {"id": 2, "name": "viewer", "is_default": False},
            ])
            connection.execute(permissions.insert(), [
                {"id": 1, "code": "roles.manage", "description": "legacy permission"},
                {"id": 2, "code": "app.feature.carbon_stock", "description": "preexisting feature code"},
            ])
            connection.execute(role_permissions.insert(), [
                {"role_id": 1, "permission_id": 1},
                {"role_id": 2, "permission_id": 2},
            ])

        with engine.begin() as connection:
            _run_migration(connection, migration, "upgrade")
            role_columns = {column["name"] for column in inspect(connection).get_columns("roles")}
            assert "feature_permissions_configured" in role_columns
            viewer_permissions = connection.execute(
                select(permissions.c.code)
                .select_from(role_permissions.join(permissions))
                .where(role_permissions.c.role_id == 2)
            ).scalars().all()
            assert len([code for code in viewer_permissions if code.startswith("app.feature.")]) == 7

        with engine.begin() as connection:
            _run_migration(connection, migration, "downgrade")
            role_columns = {column["name"] for column in inspect(connection).get_columns("roles")}
            assert "feature_permissions_configured" not in role_columns
            assert connection.execute(select(roles.c.name).where(roles.c.name == "viewer")).scalar_one() == "viewer"
            assert connection.execute(select(permissions.c.description).where(permissions.c.code == "roles.manage")).scalar_one() == "legacy permission"
            assert connection.execute(
                select(role_permissions.c.permission_id).where(
                    role_permissions.c.role_id == 1,
                    role_permissions.c.permission_id == 1,
                )
            ).scalar_one() == 1
            assert connection.execute(select(permissions.c.description).where(permissions.c.code == "app.feature.carbon_stock")).scalar_one() == "preexisting feature code"
            viewer_permission_codes = connection.execute(
                select(permissions.c.code)
                .select_from(role_permissions.join(permissions))
                .where(role_permissions.c.role_id == 2)
            ).scalars().all()
            assert viewer_permission_codes == ["app.feature.carbon_stock"]
    finally:
        engine.dispose()
