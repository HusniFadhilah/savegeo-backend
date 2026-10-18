"""In-process throttling for credential and password-reset endpoints.

Failed logins are counted per ``(client IP, account identifier)`` and, with a
higher ceiling, per client IP. Keying on the identifier keeps one noisy client
behind a shared reverse-proxy address from locking every account at once,
while the per-IP ceiling still slows credential stuffing across many names.

The state is per worker process. Production should also enforce a shared limit
at the reverse proxy and set ``FORWARDED_ALLOW_IPS`` so the real client address
is used (see ``.env.production.example``).
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

_MAX_TRACKED_KEYS = 50_000


class SlidingWindowCounter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float, window: float) -> deque[float]:
        events = self._events[key]
        while events and now - events[0] >= window:
            events.popleft()
        return events

    def retry_after(self, key: str, limit: int, window: float) -> int:
        """Seconds until ``key`` may try again, or 0 when it is under ``limit``."""
        if limit <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, now, window)
            if len(events) < limit:
                if not events:
                    self._events.pop(key, None)
                return 0
            return max(1, math.ceil(window - (now - events[0])))

    def record(self, key: str, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._events) >= _MAX_TRACKED_KEYS:
                for stale in [k for k, v in self._events.items() if not v or now - v[-1] >= window]:
                    del self._events[stale]
            self._prune(key, now, window).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_login_failures = SlidingWindowCounter()
_reset_requests = SlidingWindowCounter()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many(retry_after: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


def _keys(request: Request, scope: str, identifier: str) -> tuple[str, str]:
    client = _client(request)
    return f"{scope}:ip:{client}", f"{scope}:acct:{client}:{identifier.strip().casefold()}"


def ensure_login_allowed(request: Request, scope: str, identifier: str) -> None:
    settings = get_settings()
    window = settings.auth_throttle_window_seconds
    ip_key, account_key = _keys(request, scope, identifier)
    wait = max(
        _login_failures.retry_after(account_key, settings.login_failures_per_account, window),
        _login_failures.retry_after(ip_key, settings.login_failures_per_ip, window),
    )
    if wait:
        raise _too_many(wait, "Terlalu banyak percobaan login gagal. Coba lagi nanti.")


def record_login_failure(request: Request, scope: str, identifier: str) -> None:
    window = get_settings().auth_throttle_window_seconds
    for key in _keys(request, scope, identifier):
        _login_failures.record(key, window)


def record_login_success(request: Request, scope: str, identifier: str) -> None:
    _login_failures.reset(_keys(request, scope, identifier)[1])


def ensure_password_reset_request_allowed(request: Request, scope: str, identifier: str) -> None:
    """Count every reset request, because each one may send an email."""
    settings = get_settings()
    window = settings.auth_throttle_window_seconds
    ip_key, account_key = _keys(request, scope, identifier)
    wait = max(
        _reset_requests.retry_after(account_key, settings.password_reset_requests_per_account, window),
        _reset_requests.retry_after(ip_key, settings.password_reset_requests_per_ip, window),
    )
    if wait:
        raise _too_many(wait, "Terlalu banyak permintaan reset password. Coba lagi nanti.")
    _reset_requests.record(account_key, window)
    _reset_requests.record(ip_key, window)


def reset_all() -> None:
    """Test helper: forget every recorded attempt."""
    _login_failures.clear()
    _reset_requests.clear()
