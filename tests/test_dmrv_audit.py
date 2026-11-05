import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models.dmrv_audit import DmrvAuditEvent
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    DmrvAuditEvent.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(DmrvAuditEvent)).scalar_one()


def test_audit_is_flushed_not_committed(db):
    event = record_audit(
        db, principal=Principal("admin", 1), action="project.create", resource_type="project",
        resource_id="p-1", project_id="p-1", after={"name": "Pilot"}, request_id="req-1",
    )
    assert event.id is not None
    assert _count(db) == 1
    db.rollback()
    assert _count(db) == 0


def test_audit_failure_propagates(db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("flush failed")

    monkeypatch.setattr(db, "flush", boom)
    with pytest.raises(RuntimeError, match="flush failed"):
        record_audit(
            db, principal=Principal("admin", 1), action="project.create",
            resource_type="project", resource_id="p-1", project_id="p-1",
        )


def test_event_records_principal_type_and_id(db):
    record_audit(db, principal=Principal("admin", 1), action="a", resource_type="project", resource_id="p", project_id="p")
    record_audit(db, principal=Principal("user", 1), action="b", resource_type="project", resource_id="p", project_id="p")
    db.commit()
    rows = db.execute(select(DmrvAuditEvent.actor_type, DmrvAuditEvent.actor_id).order_by(DmrvAuditEvent.id)).all()
    assert rows == [("admin", 1), ("user", 1)]
