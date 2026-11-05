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

from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_project import DmrvProject

JOB_TABLES = {"dmrv_jobs", "dmrv_snapshots"}
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0041_dmrv_jobs.py"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://")
    for model in (DmrvProject, DmrvJob, DmrvSnapshot):
        model.__table__.create(engine)
    yield engine
    engine.dispose()


def _job(job_id="j-1", **over):
    values = {"id": job_id, "project_id": "p-1", "kind": "field_summary", "payload": {}, "created_by": "admin:1"}
    values.update(over)
    return DmrvJob(**values)


def _project(db):
    db.add(DmrvProject(id="p-1", name="A"))
    db.commit()


def test_job_defaults(engine):
    with Session(engine) as db:
        _project(db)
        db.add(_job())
        db.commit()
        job = db.get(DmrvJob, "j-1")
        assert (job.status, job.attempts, job.max_attempts, job.cancel_requested) == ("queued", 0, 3, False)
        assert job.progress == {} and job.lease_owner is None and job.run_after is not None and job.created_at is not None


def test_idempotency_key_is_unique_per_creator(engine):
    with Session(engine) as db:
        _project(db)
        db.add_all([_job("a", idempotency_key="k"), _job("b", idempotency_key="k", created_by="user:1")])
        db.commit()
        db.add(_job("c", idempotency_key="k"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_jobs_without_a_key_do_not_collide(engine):
    with Session(engine) as db:
        _project(db)
        db.add_all([_job("a"), _job("b")])
        db.commit()


def test_snapshot_hash_is_unique_per_project(engine):
    with Session(engine) as db:
        _project(db)
        db.add(DmrvProject(id="p-2", name="B"))

        def snap(snap_id, project_id):
            return DmrvSnapshot(id=snap_id, project_id=project_id, kind="field_summary", inputs={}, outputs={},
                                snapshot_hash="h" * 64, created_by="admin:1")

        db.add_all([snap("s1", "p-1"), snap("s2", "p-2")])
        db.commit()
        db.add(snap("s3", "p-1"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_migration_creates_and_drops_tables():
    spec = importlib.util.spec_from_file_location("dmrv_0041", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0041_dmrv_jobs" and module.down_revision == "0040_dmrv_field_data"
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        DmrvProject.__table__.create(conn)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert JOB_TABLES <= set(inspect(conn).get_table_names())
        assert {"lease_owner", "lease_expires_at", "cancel_requested", "run_after"} <= {
            c["name"] for c in inspect(conn).get_columns("dmrv_jobs")
        }
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
        assert not JOB_TABLES & set(inspect(conn).get_table_names())
    engine.dispose()
