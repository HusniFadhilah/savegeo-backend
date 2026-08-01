from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import engine


def _db_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_available() -> bool:
    return _db_reachable()


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c
