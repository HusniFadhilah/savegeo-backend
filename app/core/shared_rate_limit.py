"""Atomic fixed-window quotas shared by API processes through PostgreSQL."""
from __future__ import annotations

import hashlib
import math
import time
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

_cleanup_lock = Lock()
_cleanup_at = 0.0


def consume(key: str, limit: int, window_seconds: int, *, connection_engine=None) -> int:
    """Consume one request atomically, returning Retry-After on exhaustion.

    Key material is hashed before storage. PostgreSQL enforces the quota with
    an atomic conditional upsert, including simultaneous callers on two workers.
    A database failure closes costly paths rather than granting unlimited access.
    """
    if limit <= 0:
        return 0
    if window_seconds < 1:
        raise HTTPException(status_code=503, detail="Invalid quota window")
    if connection_engine is None:
        from app.db.session import engine
        connection_engine = engine
    now = int(time.time())
    start = now // window_seconds * window_seconds
    expires = start + window_seconds
    digest = hashlib.sha256(f"{window_seconds}:{key}".encode()).hexdigest()
    try:
        with connection_engine.begin() as db:
            allowed = db.execute(text("""
                INSERT INTO request_rate_buckets (bucket_key, window_start, hits, expires_at)
                VALUES (:key, :start, 1, :expires)
                ON CONFLICT (bucket_key, window_start) DO UPDATE
                SET hits = request_rate_buckets.hits + 1
                WHERE request_rate_buckets.hits < :limit
                RETURNING hits
            """), {"key": digest, "start": start, "expires": expires, "limit": limit}).scalar()
            global _cleanup_at
            if time.monotonic() - _cleanup_at >= 60 and _cleanup_lock.acquire(blocking=False):
                try:
                    db.execute(text("DELETE FROM request_rate_buckets WHERE (bucket_key, window_start) IN (SELECT bucket_key, window_start FROM request_rate_buckets WHERE expires_at < :now LIMIT 500)"), {"now": now})
                    _cleanup_at = time.monotonic()
                finally:
                    _cleanup_lock.release()
        return 0 if allowed is not None else max(1, math.ceil(expires - time.time()))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Request quota service unavailable", headers={"Retry-After": "5"}) from exc
