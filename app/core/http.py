"""HTTP hardening shared by the FastAPI application.

The middleware in this module is deliberately small and response-only: it does
not change route URLs or authentication semantics.  ``X-Request-ID`` gives
operators a stable correlation key without accepting arbitrary log-injection
content from a caller.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
import uuid
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline browser and transport protections to every response."""

    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self._rate_windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def _problem(
        self, request: Request, status_code: int, detail: str, headers: dict[str, str] | None = None
    ):
        from starlette.responses import JSONResponse

        return JSONResponse(
            status_code=status_code,
            content={
                "type": f"urn:savegeo:problem:{status_code}",
                "title": "Request error",
                "status": status_code,
                "detail": detail,
                "instance": request.url.path,
                "request_id": getattr(request.state, "request_id", None),
                "error": detail,
            },
            headers=headers,
            media_type="application/problem+json",
        )

    def _rate_limited(self, request: Request) -> bool:
        if request.url.path in {"/api/health", "/docs", "/redoc", "/openapi.json"}:
            return False
        if not request.url.path.startswith("/api/"):
            return False

        # Leaflet issues one request per visible raster tile. Keep these
        # authenticated CAMS reads inside their own, higher budget so a global
        # layer cannot starve ordinary API calls (e.g. region statistics).
        is_map_tile = (
            request.method in {"GET", "HEAD"}
            and request.url.path.startswith("/api/disasters/wildfires/events/")
            and "/emissions/tiles/" in request.url.path
        )
        bucket = "map_tiles" if is_map_tile else "api"
        limit = (
            self.settings.rate_limit_map_tiles_per_minute
            if is_map_tile
            else self.settings.rate_limit_requests_per_minute
        )
        if limit <= 0:
            return False

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = self._rate_windows[(client, bucket)]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(now)
        return False

    def _csrf_rejected(self, request: Request) -> bool:
        if not self.settings.csrf_protection_enabled or request.method in {"GET", "HEAD", "OPTIONS"}:
            return False
        has_cookie = any(request.cookies.get(name) for name in ("savegeo_admin_session", "savegeo_user_session"))
        origin = request.headers.get("origin")
        is_auth = request.url.path.startswith(("/api/auth/", "/api/admin/auth/"))
        if not has_cookie and not is_auth:
            return False
        if origin:
            return origin not in self.settings.allowed_origins_list
        if not has_cookie:
            return False  # Non-browser login clients do not use ambient credentials.
        referer = request.headers.get("referer")
        if referer:
            try:
                parsed = urlsplit(referer)
            except ValueError:
                return True
            return f"{parsed.scheme}://{parsed.netloc}" not in self.settings.allowed_origins_list
        # Cookie mutations must carry evidence of a trusted browser origin.
        return True

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        request.state.request_id = request_id

        if self._rate_limited(request):
            response = self._problem(request, 429, "Rate limit exceeded", {"Retry-After": "60"})
        elif self._csrf_rejected(request):
            response = self._problem(request, 403, "Origin is not allowed for cookie-authenticated requests")
        else:
            response = await call_next(request)
        if request.url.path.startswith(self.settings.api_prefix + "/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        # Swagger/ReDoc require their own scripts/styles.  API and application
        # responses still receive a restrictive CSP, while documentation keeps
        # its existing behavior and remains reachable for integration tooling.
        if request.url.path not in {"/docs", "/redoc", "/openapi.json"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )
        if self.settings.app_env.lower() in {"prod", "production", "staging"}:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
