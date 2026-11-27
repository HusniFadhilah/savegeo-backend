"""Regression cases for remaining recommendations in the signed review."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models.analysis_job import AnalysisJob
from app.db.models.provider_cache import ProviderCache
from app.services import analysis_queue as queue, location_provider as locations
from app.services import samgeo_service
from app.api.routes import carbon_calibration


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
    result = locations.imagery_date_metadata(-6.9781234567, 110.412345678)
    assert captured["geometry"] == "110.41,-6.98"
    assert "110.412345678" not in str(captured)
    disclosure = result["data_sharing_notice"]
    assert disclosure["provider"] == "Esri World Imagery"
    assert disclosure["coordinate_precision_degrees"] == 0.01
    assert "0,01 derajat" in disclosure["notice"]["id"]


def test_place_search_response_cache_expires_after_five_minutes(monkeypatch):
    captured = {}

    def fake_cache(provider, params, fetch, **kwargs):
        captured.update(provider=provider, params=params, **kwargs)
        return []

    monkeypatch.setattr(locations, "cached_lookup", fake_cache)
    locations.search_places("Semarang", limit=5)

    assert captured["provider"] == "nominatim"
    assert captured["ttl"] == 300


def test_optional_model_worker_does_not_inherit_application_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:private@example.test/db")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:private@example.test")
    monkeypatch.setenv("PATH", "C:/python")

    env = samgeo_service._worker_environment(tmp_path / "checkpoints")

    assert env["PATH"] == "C:/python"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert Path(env["HF_HOME"]) == tmp_path / "checkpoints" / "huggingface"
    assert "DATABASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert "private" not in str(env)


def test_optional_model_worker_uses_bounded_sanitized_subprocess(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return object()

    monkeypatch.setattr(samgeo_service.subprocess, "run", fake_run)
    workdir = tmp_path / "job"
    checkpoints = tmp_path / "checkpoints"
    result = samgeo_service._execute_worker(["python", "worker.py"], folder=workdir, checkpoint_dir=checkpoints)

    assert result is not None
    assert captured["cwd"] == str(workdir)
    assert captured["timeout"] == 900
    assert captured["stdin"] is samgeo_service.subprocess.DEVNULL
    assert captured["env"]["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert captured["capture_output"] is True


def test_carbon_calibration_job_is_registered_before_worker_submission(monkeypatch):
    submitted = []

    class Executor:
        def submit(self, function, *args):
            submitted.append((function, args))

    monkeypatch.setattr(carbon_calibration, "executor", Executor())
    carbon_calibration.jobs.clear()

    response = carbon_calibration._queue_dataset_job(
        SimpleNamespace(id=17, dataset_id="field-set-17"), "extract",
    )

    job_id = response["job_id"]
    assert response["status"] == "queued"
    assert carbon_calibration.jobs[job_id] == {
        "status": "queued",
        "progress": 0,
        "type": "extract",
        "dataset_id": "field-set-17",
    }
    assert submitted == [(carbon_calibration._extract_dataset, (17, job_id))]
