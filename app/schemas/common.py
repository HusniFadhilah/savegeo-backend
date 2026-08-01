"""Shared response envelopes. Most legacy routes return a bare `{"error": "..."}` on
failure with no consistent success wrapper — we keep that (frontend already tolerates
unwrapped success payloads per `docs/api-contracts.md`), and standardize only the
error shape used by our global exception handlers.
"""
from __future__ import annotations

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error: str


class BilingualErrorDetail(BaseModel):
    message_key: str
    params: dict = {}
    id: str
    en: str


class BilingualErrorResponse(BaseModel):
    error: BilingualErrorDetail
