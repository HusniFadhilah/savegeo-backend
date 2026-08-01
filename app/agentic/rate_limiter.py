"""
rate_limiter.py — In-memory sliding-window rate limiter for the AI endpoint.
Limits are per client IP, configurable via SystemConfig DB (ai.rate_limit_*).
Thread-safe; stores state in module-level dict (resets on server restart).
"""
import time
from collections import defaultdict
from threading import Lock

_store: dict = defaultdict(lambda: {"min": [], "day": []})
_lock  = Lock()

# Config cache — refreshed every 30 s so admin changes apply quickly
_cfg_cache: dict = {}
_cfg_ts: float   = 0.0
_CFG_TTL: float  = 30.0


def _get_limits() -> dict:
    global _cfg_cache, _cfg_ts
    now = time.time()
    if now - _cfg_ts < _CFG_TTL and _cfg_cache:
        return _cfg_cache

    defaults = {
        "rpm":          10,    # requests per minute
        "daily":        200,   # requests per day
        "max_chars":    4000,  # max prompt length (chars)
        "max_file_mb":  5,     # max attachment size (MB)
    }
    try:
        from app.db.models.system_config import SystemConfig
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            def _i(key, d):
                row = db.query(SystemConfig).filter_by(key=key).first()
                try:
                    return max(1, int(float(row.value))) if row and row.value else d
                except Exception:
                    return d

            _cfg_cache = {
                "rpm":         _i("ai.rate_limit_rpm",         defaults["rpm"]),
                "daily":       _i("ai.rate_limit_daily",       defaults["daily"]),
                "max_chars":   _i("ai.rate_limit_max_chars",   defaults["max_chars"]),
                "max_file_mb": _i("ai.rate_limit_max_file_mb", defaults["max_file_mb"]),
            }
        finally:
            db.close()
    except Exception:
        _cfg_cache = defaults.copy()

    _cfg_ts = now
    return _cfg_cache


def get_limits() -> dict:
    """Return current limits dict (for frontend info)."""
    return _get_limits()


def check(ip: str) -> tuple:
    """
    Check rate limits for ip.
    Returns (allowed: bool, error_message: str, retry_after_seconds: int).
    """
    limits = _get_limits()
    rpm    = limits["rpm"]
    daily  = limits["daily"]
    now    = time.time()

    with _lock:
        b = _store[ip]
        b["min"] = [t for t in b["min"] if now - t < 60]
        b["day"] = [t for t in b["day"] if now - t < 86400]

        if len(b["min"]) >= rpm:
            retry = max(1, int(61 - (now - b["min"][0])))
            return (
                False,
                f"Terlalu banyak permintaan ({rpm}/menit). Coba lagi dalam {retry} detik.",
                retry,
            )

        if len(b["day"]) >= daily:
            retry = max(1, int(86401 - (now - b["day"][0])))
            return False, f"Batas harian ({daily} permintaan) tercapai. Coba besok.", retry

        b["min"].append(now)
        b["day"].append(now)
        return True, "", 0


def reset(ip: str) -> None:
    """Admin: clear rate-limit state for an IP."""
    with _lock:
        _store.pop(ip, None)


def stats() -> dict:
    """Return current store stats (admin dashboard use)."""
    with _lock:
        return {ip: {k: len(v) for k, v in b.items()} for ip, b in _store.items()}
