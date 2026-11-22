"""Quota concurrency and cross-client isolation against real disposable PostgreSQL."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.shared_rate_limit import consume
from app.db.models.request_rate_bucket import RequestRateBucket


def test_atomic_quota_shared_by_connections_and_clients():
    url = os.environ.get("DMRV_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DMRV_TEST_DATABASE_URL must name an isolated test database")
    schema = "quota_test_" + uuid.uuid4().hex
    root = create_engine(url)
    with root.begin() as db:
        db.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"}, pool_size=12)
    try:
        RequestRateBucket.__table__.create(engine)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: consume("user:1", 10, 3600, connection_engine=engine), range(30)))
            cost_results = list(pool.map(
                lambda _: consume("ai:daily-cost:global", 17, 3600, units=5, connection_engine=engine), range(30)
            ))
        assert results.count(0) == 10
        assert cost_results.count(0) == 3
        assert consume("user:2", 10, 3600, connection_engine=engine) == 0
        # A new engine (a new worker) sees the same exhausted quota.
        other = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            assert consume("user:1", 10, 3600, connection_engine=other) > 0
            assert consume("ai:daily-cost:global", 17, 3600, units=5, connection_engine=other) > 0
        finally:
            other.dispose()
        with engine.connect() as db:
            rows = db.execute(select(RequestRateBucket.__table__)).mappings().all()
            assert len(rows) == 3 and sorted(row['hits'] for row in rows) == [1, 10, 15]
            assert all(len(row['bucket_key']) == 64 and 'user' not in row['bucket_key'] for row in rows)
    finally:
        engine.dispose()
        with root.begin() as db:
            db.execute(DropSchema(schema, cascade=True))
        root.dispose()
