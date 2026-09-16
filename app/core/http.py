"""HTTP hardening shared by the FastAPI application.

The middleware in this module is deliberately small and response-only: it does
not change route URLs or authentication semantics.  ``X-Request-ID`` gives
operators a stable correlation key without accepting arbitrary log-injection
content from a caller.
"""

from __future__ import annotations

import re
import uuid

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

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
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
