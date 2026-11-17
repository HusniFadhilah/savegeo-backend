"""Regression cases for remaining recommendations in the signed review."""
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models.analysis_job import AnalysisJob
from app.db.models.provider_cache import ProviderCache
from app.services import analysis_queue as queue, location_provider as locations


@pytest.fixture
def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    AnalysisJob.__table__.create(engine)
    ProviderCache.__table__.create(engine)
    factory = sessionmaker(engine)
    yield factory
    engine.dispose()


def test_queue_recovers_after_restart_with_fenced_attempt(database):
    with database() as db:
        job, created = queue.prepare(db, "vegetation", {"year": 2026}, "user:1", idempotency_key="request-1")
        assert created
        job_id = job.id
        queue.activate(db, job_id)
    with database() as worker_one:
        first = queue.claim(worker_one, "first", now=time.time() - 120)
        assert first.id == job_id
    with database() as db:
        assert queue.reap(db) == 1
        second = queue.claim(db, "second")
        assert second.id == job_id and second.attempts == 2
    with database() as stale:
        assert not queue.heartbeat(stale, job_id, "first")
        with pytest.raises(RuntimeError, match="lease"):
            queue.write_document(stale, job_id, {"status": "succeeded"}, "first")
    with database() as current:
        assert queue.heartbeat(current, job_id, "second")
        assert queue.write_document(current, job_id, {"status": "succeeded", "result": {"ok": True}}, "second")
    with database() as restarted:
        stored = restarted.get(AnalysisJob, job_id)
        assert stored.document["owner"] == "user:1"
        assert stored.document["result"] == {"ok": True}
        assert queue.claim(restarted, "third") is None


def test_queue_idempotency_is_owner_bound_and_rejects_changed_payload(database):
    with database() as db:
        first, _ = queue.prepare(db, "carbon", {"year": 2026}, "user:1", idempotency_key="same-key")
        replay, created = queue.prepare(db, "carbon", {"year": 2026}, "user:1", idempotency_key="same-key")
        assert replay.id == first.id and not created
        other, created = queue.prepare(db, "carbon", {"year": 2026}, "admin:1", idempotency_key="same-key")
        assert other.id != first.id and created
        with pytest.raises(HTTPException) as error:
            queue.prepare(db, "carbon", {"year": 2025}, "user:1", idempotency_key="same-key")
        assert error.value.status_code == 409


def test_queue_cancel_and_final_states_are_atomic_and_owner_scoped(database):
    with database() as db:
        job, _ = queue.prepare(db, "carbon", {}, "user:1")
        queue.activate(db, job.id)
        job_id = job.id
    with database() as db:
        with pytest.raises(HTTPException) as error:
            queue.cancel(db, job_id, "admin:1")
        assert error.value.status_code == 404
    with database() as db:
        queue.claim(db, "token")
        assert queue.cancel(db, job_id, "user:1")["status"] == "cancel_requested"
    with database() as db:
        assert not queue.heartbeat(db, job_id, "token")
        with pytest.raises(RuntimeError):
            queue.write_document(db, job_id, {"status": "succeeded"}, "token")


def test_queue_has_global_admission_limit_and_bounded_retries(database):
    with database() as db:
        for _ in range(24):
            job, _ = queue.prepare(db, "carbon", {}, "user:1")
            queue.activate(db, job.id)
        with pytest.raises(HTTPException) as error:
            queue.prepare(db, "carbon", {}, "user:2")
        assert error.value.status_code == 429
    with database() as db:
        job = queue.claim(db, "last-attempt", now=time.time() - 120)
        job.attempts = 3
        db.commit()
        job_id = job.id
        queue.reap(db)
        assert db.get(AnalysisJob, job_id).status == "failed"


def test_location_cache_and_interval_are_shared_across_sessions(database, monkeypatch):
    monkeypatch.setattr(locations, "SessionLocal", database)
    monkeypatch.setattr(locations.time, "time", lambda: 1000)
    calls = []
    def fetch():
        calls.append(True)
        return [{"display_name": "Semarang"}]
    assert locations.cached_lookup("nominatim", {"q": "Semarang"}, fetch, interval=1.1)
    assert locations.cached_lookup("nominatim", {"q": "Semarang"}, fetch, interval=1.1)
    assert len(calls) == 1
    with pytest.raises(HTTPException) as error:
        locations.cached_lookup("nominatim", {"q": "Bandung"}, fetch, interval=1.1)
    assert error.value.status_code == 429 and error.value.headers["Retry-After"] == "2"
    with database() as db:
        # A query isn't stored as a key or parameter alongside identity.
        assert all("Semarang" not in row.key for row in db.query(ProviderCache))


def test_location_failed_request_still_consumes_global_interval(database, monkeypatch):
    monkeypatch.setattr(locations, "SessionLocal", database)
    monkeypatch.setattr(locations.time, "time", lambda: 1000)
    with pytest.raises(HTTPException) as error:
        locations.cached_lookup("nominatim", {}, lambda: 1 / 0, interval=1.1)
    assert error.value.status_code == 502
    with pytest.raises(HTTPException) as error:
        locations.cached_lookup("nominatim", {"q": "Other"}, lambda: [], interval=1.1)
    assert error.value.status_code == 429


def test_imagery_proxy_coarsens_coordinates_before_egress(monkeypatch):
    captured = {}
    def fake_cache(provider, params, fetch, **kwargs):
        captured.update(params)
        return {"results": []}
    monkeypatch.setattr(locations, "cached_lookup", fake_cache)
    locations.imagery_date_metadata(-6.9781234567, 110.412345678)
    assert captured["geometry"] == "110.41,-6.98"
    assert "110.412345678" not in str(captured)
