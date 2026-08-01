"""Shared FastAPI dependencies used across route modules."""
from __future__ import annotations

from fastapi import HTTPException, Request, status


def require_ee(request: Request) -> None:
    """Gate GEE-dependent routes. Mirrors legacy `app.config.get("EE_INITIALIZED")` checks."""
    if not getattr(request.app.state, "ee_initialized", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Earth Engine belum diinisialisasi. Cek kredensial di Admin Panel.",
        )
