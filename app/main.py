"""FastAPI application entrypoint.

Startup mirrors legacy app.py's `with app.app_context(): _sync_config_from_db();
import_legacy_models(...); init_ee_from_db()` sequence, minus the automatic legacy
model import (moved to an explicit `scripts/import_legacy_models.py` per the
migration plan - not run automatically on every boot).
"""

from __future__ import annotations

import logging
import hashlib
import secrets
from contextlib import asynccontextmanager
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.services.dmrv.errors import register_dmrv_error_handler
from app.core.config import get_settings
from app.core.logging_security import install_secret_redaction
from app.core.version import get_product_version
from app.core.cors import setup_cors
from app.core.http import SecurityHeadersMiddleware, problem_instance
from app.db.session import SessionLocal
from app.services import gee_service
from app.services.arcgis_service import configure_arcgis_from_db

logging.basicConfig(level=logging.INFO)
install_secret_redaction()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ee_initialized = False
    db = SessionLocal()
    try:
        try:
            configure_arcgis_from_db(db)
        except Exception as exc:  # noqa: BLE001 - retain env defaults if DB config is unavailable
            logger.warning("ArcGIS database configuration skipped at startup: %s", exc)
        app.state.ee_initialized = gee_service.initialize_ee(db)
    except Exception as exc:  # noqa: BLE001 - startup must not crash the app if GEE/DB is unreachable
        logger.warning("GEE initialization skipped at startup: %s", exc)
    finally:
        db.close()
    yield


settings = get_settings()

app = FastAPI(
    title="SAVEGEO / GEOMOKA API",
    description="Carbon stock, vegetation, land cover, and disaster analysis platform - FastAPI + Supabase Postgres backend.",
    version=get_product_version(),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_docs_basic = HTTPBasic(auto_error=False)


def require_docs_password(credentials: HTTPBasicCredentials | None = Depends(_docs_basic)) -> None:
    """Keep the schema and both documentation pages closed by default."""
    username = settings.api_docs_username
    password = settings.api_docs_password.get_secret_value()
    if not username or len(password) < 24:
        raise HTTPException(status_code=503, detail="API documentation is not configured")
    supplied_user = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    # Compare fixed-length digests to support Unicode without exposing secrets.
    def digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()
    valid_user = secrets.compare_digest(digest(supplied_user), digest(username))
    valid_password = secrets.compare_digest(digest(supplied_password), digest(password))
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": 'Basic realm="SaveGeo API docs"'})


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_docs_password)])
def private_openapi():
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_docs_password)])
def private_swagger(request: Request):
    return get_swagger_ui_html(openapi_url=request.scope.get("root_path", "").rstrip("/") + "/openapi.json", title=app.title + " - Swagger UI")


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_docs_password)])
def private_redoc(request: Request):
    return get_redoc_html(openapi_url=request.scope.get("root_path", "").rstrip("/") + "/openapi.json", title=app.title + " - ReDoc")

setup_cors(app, settings)
app.add_middleware(SecurityHeadersMiddleware, settings=settings)
backend_root = Path(__file__).resolve().parent.parent
disaster_raster_dir = Path(settings.disaster_raster_dir)
if not disaster_raster_dir.is_absolute():
    disaster_raster_dir = backend_root / disaster_raster_dir
disaster_thumbnail_dir = disaster_raster_dir / "thumbnails"
disaster_thumbnail_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/disaster-thumbnails",
    StaticFiles(directory=str(disaster_thumbnail_dir)),
    name="disaster-thumbnails",
)
app.include_router(api_router, prefix=settings.api_prefix)
register_dmrv_error_handler(app)


@app.get("/", tags=["system"])
def root() -> dict[str, str]:
    """Small public landing response for the backend domain."""
    return {
        "name": "SAVEGEO / GEOMOKA API",
        "status": "ok",
        "health": f"{settings.api_prefix}/health",
        "docs": "/docs",
    }


def _problem_response(
    request: Request,
    status_code: int,
    detail: object,
    *,
    errors: object = None,
    headers: Mapping[str, str] | None = None,
):
    """Return RFC 9457-compatible JSON while retaining the legacy ``error`` key."""
    instance = problem_instance(request)
    detail_text = detail if isinstance(detail, str) else "Request failed"
    code = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        501: "not_implemented",
        502: "upstream_error",
        503: "service_unavailable",
        504: "upstream_timeout",
    }.get(status_code, f"http_{status_code}")
    if isinstance(detail, dict):
        candidate = detail.get("code")
        if isinstance(candidate, str) and candidate.replace("_", "").isalnum() and len(candidate) <= 80:
            code = candidate
        if status_code < 500 and isinstance(detail.get("message"), str):
            detail_text = detail["message"]
    if status_code >= 500:
        detail_text = "An internal server error occurred. Please try again later."
    body: dict[str, object] = {
        "type": f"urn:savegeo:problem:{status_code}",
        "title": "Request error" if status_code < 500 else "Internal server error",
        "status": status_code,
        "detail": detail_text,
        # Prefer the registered route template so a 404 does not reveal whether
        # a private project identifier exists to the caller.
        "instance": instance,
        "request_id": getattr(request.state, "request_id", None),
        # Existing frontend and integrations consume this field. Keep it as a
        # non-breaking extension while clients migrate to Problem Details.
        "error": detail_text,
    }
    if errors is not None:
        body["errors"] = jsonable_encoder(errors)
    body["code"] = code
    if isinstance(detail, dict):
        # Preserve documented, non-sensitive agent response extensions while
        # keeping every failure in the same Problem Details envelope.
        for key in ("intent", "confidence", "needs_confirmation", "warnings", "actions", "retryable", "retry_after", "ai"):
            if key in detail:
                body[key] = jsonable_encoder(detail[key])
        if code.startswith("agent_") and isinstance(detail.get("message"), str):
            body["message"] = detail["message"][:2000]
    if (
        isinstance(detail, dict)
        and code in {"invalid_aoi", "invalid_year", "invalid_date_range", "invalid_cloud_threshold", "invalid_resolution", "analysis_preflight_blocked"}
        and isinstance(detail.get("preflight"), dict)
    ):
        body["preflight"] = jsonable_encoder(detail["preflight"])
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=headers,
        media_type="application/problem+json",
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return _problem_response(request, exc.status_code, exc.detail, headers=exc.headers)


@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Also normalize framework-generated 404/405 responses."""
    return _problem_response(request, exc.status_code, exc.detail, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Pydantic's raw input/ctx may contain passwords, tokens or key material.
    errors = [{"loc": error["loc"], "type": error["type"], "msg": "Invalid value"} for error in exc.errors()]
    return _problem_response(request, 422, "Validation error", errors=errors)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(
        "Unhandled error request_id=%s method=%s path=%s", request_id, request.method, request.url.path
    )
    return _problem_response(request, 500, "An internal server error occurred. Please try again later.")
