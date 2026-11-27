"""dMRV error type and the response body required by the API contract:
``{code, message, request_id, retryable, details}``."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.http import problem_instance


class DmrvError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: list | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or []


def project_not_found() -> DmrvError:
    """The single response for a missing project and for a non-member."""
    return DmrvError(404, "not_found", "Proyek tidak ditemukan.")


def register_dmrv_error_handler(app: FastAPI) -> None:
    @app.exception_handler(DmrvError)
    async def _handle(request: Request, exc: DmrvError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "type": f"urn:savegeo:problem:{exc.status}",
                "title": "Request error" if exc.status < 500 else "Service error",
                "status": exc.status,
                "detail": exc.message,
                "instance": problem_instance(request),
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
                "retryable": exc.retryable,
                "details": exc.details,
                # Kept for older clients while the API adopts Problem Details.
                "error": exc.message,
            },
            media_type="application/problem+json",
        )
