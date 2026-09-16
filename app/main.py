"""FastAPI application entrypoint.

Startup mirrors legacy app.py's `with app.app_context(): _sync_config_from_db();
import_legacy_models(...); init_ee_from_db()` sequence, minus the automatic legacy
model import (moved to an explicit `scripts/import_legacy_models.py` per the
migration plan - not run automatically on every boot).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.core.config import get_settings
from app.core.cors import setup_cors
from app.core.http import SecurityHeadersMiddleware
from app.db.session import SessionLocal
from app.services import gee_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ee_initialized = False
    db = SessionLocal()
    try:
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
    version="0.1.0",
    lifespan=lifespan,
)

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
    headers: dict[str, str] | None = None,
):
    """Return RFC 9457-compatible JSON while retaining the legacy ``error`` key."""
    detail_text = detail if isinstance(detail, str) else "Request failed"
    body: dict[str, object] = {
        "type": f"urn:savegeo:problem:{status_code}",
        "title": "Request error" if status_code < 500 else "Internal server error",
        "status": status_code,
        "detail": detail_text,
        "instance": request.url.path,
        "request_id": getattr(request.state, "request_id", None),
        # Existing frontend and integrations consume this field. Keep it as a
        # non-breaking extension while clients migrate to Problem Details.
        "error": detail_text,
    }
    if errors is not None:
        body["errors"] = jsonable_encoder(errors)
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
    return _problem_response(request, 422, "Validation error", errors=exc.errors())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(
        "Unhandled error request_id=%s method=%s path=%s", request_id, request.method, request.url.path
    )
    return _problem_response(request, 500, "An internal server error occurred. Please try again later.")
