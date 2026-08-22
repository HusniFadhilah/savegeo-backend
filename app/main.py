"""FastAPI application entrypoint.

Startup mirrors legacy app.py's `with app.app_context(): _sync_config_from_db();
import_legacy_models(...); init_ee_from_db()` sequence, minus the automatic legacy
model import (moved to an explicit `scripts/import_legacy_models.py` per the
migration plan - not run automatically on every boot).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.core.cors import setup_cors
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


# -- Error envelope: {"error": "..."} for most cases, matching the majority of
# legacy Flask routes (which return a bare {"error": message} dict). --
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": "Validation error", "detail": exc.errors()})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message_key": "internal_server_error",
                "params": {},
                "id": "Terjadi kesalahan pada server. Coba lagi nanti.",
                "en": "An internal server error occurred. Please try again later.",
            }
        },
    )
