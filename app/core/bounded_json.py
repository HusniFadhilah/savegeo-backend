"""Bounded JSON body parsing for endpoints that accept nested spatial payloads."""
from __future__ import annotations

import json
import math
from typing import TypeVar

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


async def read_bounded_json_object(request: Request, *, max_bytes: int = 10 * 1024 * 1024) -> dict:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Request body is too large")
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    remaining = 100_000
    stack = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > 30:
            raise HTTPException(status_code=422, detail="Request data exceeds supported complexity")
        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(status_code=422, detail="Numeric values must be finite")
        if isinstance(value, dict):
            if any(not isinstance(key, str) or len(key) > 256 for key in value):
                raise HTTPException(status_code=422, detail="Request key exceeds supported limits")
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)

    return payload


async def read_typed_json_object(request: Request, schema: type[T], *, max_bytes: int = 10 * 1024 * 1024) -> dict:
    payload = await read_bounded_json_object(request, max_bytes=max_bytes)
    return validate_typed_json_object(payload, schema)


def validate_typed_json_object(payload: dict, schema: type[T]) -> dict:
    try:
        parsed = schema.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    return parsed.model_dump(exclude_unset=True, exclude_none=True)
