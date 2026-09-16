"""CORS setup, mirrors legacy `flask_cors.CORS(app, resources={r"/api/*": ...})`."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings


def setup_cors(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "If-Match",
            "If-None-Match",
            "X-Request-ID",
        ],
        expose_headers=["ETag", "Last-Modified", "X-Request-ID"],
    )
