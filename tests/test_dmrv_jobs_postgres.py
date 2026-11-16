"""Queue concurrency on a real PostgreSQL (row locks, SKIP LOCKED). Skipped unless DMRV_TEST_DATABASE_URL is set
to an explicitly selected test database; each test uses a unique schema."""
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.models.dmrv_job import DmrvJob
from app.db.models.dmrv_project import DmrvProject
from app.db.models.organization import Organization
from app.services.dmrv import jobs

URL = os.environ.get("DMRV_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="DMRV_TEST_DATABASE_URL not set")


@pytest.fixture()
def factory():
    root = create_engine(URL)
    schema = "dmrv_queue_test_" + uuid.uuid4().hex
    with root.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = root.execution_options(schema_translate_map={None: schema})
    for model in (Organization, DmrvProject, DmrvJob):
        model.__table__.create(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        session.add(DmrvProject(id="p-1", name="A"))
        session.commit()
    yield maker
    with root.begin() as connection:
        connection.execute(DropSchema(schema, cascade=True))
    root.dispose()


def test_concurrent_workers_claim_every_job_exactly_once(factory):
    with factory() as db:
        for _ in range(40):
            jobs.enqueue(db, project_id="p-1", kind="field_summary", payload={}, created_by="admin:1")
        db.commit()

    claimed: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            with factory() as session:
                while True:
                    job = jobs.claim_next(session, name, lease_seconds=60)
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"w-{n}",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert len(claimed) == 40 and len(set(claimed)) == 40


def test_fencing_holds_across_sessions(factory):
    with factory() as a, factory() as b:
        job = jobs.enqueue(a, project_id="p-1", kind="field_summary", payload={}, created_by="admin:1")
        a.commit()
        jobs.claim_next(a, "w-A", lease_seconds=1)
        # worker A stalls; the reaper (another process) takes the job back and worker B claims it
        import datetime as dt

        future = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
        assert jobs.reap_expired(b, now=future) == 1
        assert jobs.claim_next(b, "w-B", lease_seconds=60, now=future + dt.timedelta(seconds=60)).id == job.id
        assert jobs.complete(a, job.id, "w-A", {"late": True}) is False
        assert jobs.complete(b, job.id, "w-B", {"ok": True}) is True
        with factory() as check:
            row = check.get(DmrvJob, job.id)
            assert row.status == "succeeded" and row.result == {"ok": True} and row.attempts == 2

