"""Sync SQLAlchemy engine/session (psycopg3 driver) + FastAPI `get_db` dependency.

Routes stay synchronous (`def`, not `async def`) so FastAPI runs them in its
threadpool — this matches the blocking nature of `earthengine-api`, `sklearn`,
and `rasterio`, none of which have async APIs. No point paying for an async
driver (`asyncpg`) when the rest of the request lifecycle is blocking anyway.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
