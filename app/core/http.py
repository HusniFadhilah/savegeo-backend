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
from starlette.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from fastapi import HTTPException

from app.core.config import Settings
from app.core.client_ip import get_client_ip

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# dMRV field-data uploads: 10 MB file + multipart overhead. The route re-checks the file itself; this
# rejects an oversized body on Content-Length before the multipart parser spools it to disk.
_DMRV_UPLOAD_PATH = re.compile(r"^/dmrv/projects/[^/]+/field-data/imports(?:/preview)?$")
DMRV_UPLOAD_LIMIT_BYTES = 11 * 1024 * 1024
_SHP_CONVERT_PATH = re.compile(r"^/utils/convert_shp$")
SHP_MULTIPART_BODY_LIMIT_BYTES = 51 * 1024 * 1024
SHP_JSON_BODY_LIMIT_BYTES = 68 * 1024 * 1024


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
                "title": "Request error" if status_code < 500 else "Service error",
                "status": status_code,
                "detail": detail,
                "instance": request.url.path,
                "request_id": getattr(request.state, "request_id", None),
                "error": detail,
                "code": {
                    400: "bad_request",
                    401: "unauthorized",
                    403: "forbidden",
                    404: "not_found",
                    413: "payload_too_large",
                    422: "validation_error",
                    429: "rate_limited",
                    500: "internal_error",
                    502: "upstream_error",
                    503: "service_unavailable",
                    504: "upstream_timeout",
                }.get(status_code, f"http_{status_code}"),
            },
            headers=headers,
            media_type="application/problem+json",
        )

    def _rate_limited(self, request: Request) -> bool:
        if request.url.path in {"/api/health", "/api/live", "/api/ready"}:
            return False
        is_docs = request.url.path in {"/docs", "/redoc", "/openapi.json"}
        if not is_docs and not request.url.path.startswith("/api/"):
            return False

        # Leaflet issues one request per visible raster tile. Keep these
        # authenticated CAMS reads inside their own, higher budget so a global
        # layer cannot starve ordinary API calls (e.g. region statistics).
        is_map_tile = (
            request.method in {"GET", "HEAD"}
            and request.url.path.startswith("/api/disasters/wildfires/events/")
            and "/emissions/tiles/" in request.url.path
        )
        is_region_read = (
            request.method in {"GET", "HEAD"}
            and request.url.path.startswith(f"{self.settings.api_prefix.rstrip('/')}/regions/")
        )
        bucket = "docs" if is_docs else "map_tiles" if is_map_tile else "regions" if is_region_read else "api"
        limit = (
            60 if is_docs else self.settings.rate_limit_map_tiles_per_minute
            if is_map_tile
            else self.settings.rate_limit_region_requests_per_minute
            if is_region_read
            else self.settings.rate_limit_requests_per_minute
        )
        if limit <= 0:
            return False

        client = get_client_ip(request, self.settings)
        if self.settings.app_env in {"production", "staging"}:
            from app.core.shared_rate_limit import consume
            return consume(f"http:{bucket}:{client}", limit, 60) > 0
        now = time.monotonic()
        # Bound state even when many distinct clients contact the process.
        if len(self._rate_windows) >= 50_000:
            for key in [key for key, entries in self._rate_windows.items() if not entries or now - entries[-1] >= 60]:
                del self._rate_windows[key]
            if (client, bucket) not in self._rate_windows and len(self._rate_windows) >= 50_000:
                return True
        window = self._rate_windows[(client, bucket)]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(now)
        return False

    def _upload_too_large(self, request: Request) -> bool:
        prefix = self.settings.api_prefix
        path = request.url.path
        if request.method != "POST" or not path.startswith(prefix + "/"):
            return False
        route_path = path[len(prefix):]
        is_dmrv_upload = bool(_DMRV_UPLOAD_PATH.match(route_path))
        is_shapefile_upload = bool(_SHP_CONVERT_PATH.match(route_path))
        if not is_dmrv_upload and not is_shapefile_upload:
            return False
        content_length = request.headers.get("content-length")
        if not content_length:
            return True
        try:
            body_size = int(content_length)
        except ValueError:
            return True
        if body_size < 0:
            return True
        if is_dmrv_upload:
            return body_size > DMRV_UPLOAD_LIMIT_BYTES
        is_multipart = "multipart/form-data" in request.headers.get("content-type", "").lower()
        limit = SHP_MULTIPART_BODY_LIMIT_BYTES if is_multipart else SHP_JSON_BODY_LIMIT_BYTES
        return body_size > limit

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

        quota_error = None
        try:
            limited = await run_in_threadpool(self._rate_limited, request) if self.settings.app_env in {"production", "staging"} else self._rate_limited(request)
        except HTTPException as exc:
            limited = False
            quota_error = exc
        if quota_error:
            response = self._problem(request, quota_error.status_code, "Request quota service unavailable", dict(quota_error.headers or {}))
        elif limited:
            response = self._problem(request, 429, "Rate limit exceeded", {"Retry-After": "60"})
        elif self._csrf_rejected(request):
            response = self._problem(request, 403, "Origin is not allowed for cookie-authenticated requests")
        elif self._upload_too_large(request):
            if _DMRV_UPLOAD_PATH.match(request.url.path[len(self.settings.api_prefix):]):
                response = JSONResponse(  # same body as every other dMRV error (see services/dmrv/errors.py)
                    status_code=413,
                    content={
                        "code": "file_too_large", "message": "Ukuran berkas maksimal 10 MB.",
                        "request_id": request_id, "retryable": False, "details": [],
                    },
                )
            else:
                response = self._problem(request, 413, "Request body exceeds the configured upload limit")
        else:
            response = await call_next(request)
        if request.url.path.startswith(self.settings.api_prefix + "/") or request.url.path in {"/docs", "/redoc", "/openapi.json"}:
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
