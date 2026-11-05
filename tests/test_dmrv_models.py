import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models.dmrv_audit import DmrvAuditEvent, DmrvIdempotencyKey
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_project import DmrvProject, ProjectMember

DMRV_TABLES = {
    "dmrv_projects",
    "dmrv_project_members",
    "dmrv_boundary_versions",
    "dmrv_audit_events",
    "dmrv_idempotency_keys",
}
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0039_dmrv_foundation.py"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://")
    for model in (DmrvProject, ProjectMember, BoundaryVersion, DmrvAuditEvent, DmrvIdempotencyKey):
        model.__table__.create(engine)
    yield engine
    engine.dispose()


def _project(db: Session) -> DmrvProject:
    project = DmrvProject(id="p-1", name="Pilot")
    db.add(project)
    db.commit()
    return project


def test_project_defaults(engine):
    with Session(engine) as db:
        project = _project(db)
        assert project.activity_type == "ARR"
        assert project.status == "draft"
        assert project.revision == 1
        assert project.created_at is not None


def test_member_unique_per_principal(engine):
    with Session(engine) as db:
        _project(db)
        db.add(ProjectMember(project_id="p-1", principal_type="user", principal_id=1, role="analyst", added_by="admin:1"))
        db.add(ProjectMember(project_id="p-1", principal_type="admin", principal_id=1, role="project_owner", added_by="admin:1"))
        db.commit()
        db.add(ProjectMember(project_id="p-1", principal_type="user", principal_id=1, role="reviewer", added_by="admin:1"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_boundary_version_unique_per_project(engine):
    def boundary(version):
        return BoundaryVersion(
            id=f"b-{version}-{id(object())}", project_id="p-1", version=version,
            original_geojson={}, validated_geojson={}, area_ha=1.0,
            area_method="geodesic_wgs84_pyproj", checksum="0" * 64, created_by="admin:1", reason="initial",
        )

    with Session(engine) as db:
        _project(db)
        db.add(boundary(1))
        db.commit()
        db.add(boundary(1))
        with pytest.raises(IntegrityError):
            db.commit()


def test_idempotency_key_unique_per_principal(engine):
    def key(principal):
        return DmrvIdempotencyKey(
            principal_key=principal, key="k1", request_hash="h", response_status=201, response_body={"id": "p-1"}
        )

    with Session(engine) as db:
        db.add(key("user:1"))
        db.add(key("admin:1"))
        db.commit()
        db.add(key("user:1"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_migration_creates_and_drops_tables():
    spec = importlib.util.spec_from_file_location("dmrv_0039", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0039_dmrv_foundation"
    assert module.down_revision == "0038_ilham_full_name"

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert DMRV_TABLES <= set(inspect(conn).get_table_names())
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
        assert not DMRV_TABLES & set(inspect(conn).get_table_names())
    engine.dispose()
