from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from app.core.bounded_json import read_bounded_json_object, read_typed_json_object
from app.schemas.agent_request import AgentRequest
from app.schemas.analysis_jobs import AnalysisJobRequest
from app.schemas.disaster_requests import EventMapRequest, FireImportRequest
from app.schemas.carbon_analysis import CarbonAnalysisRequest


def _request(body: bytes, *, content_length: str | None = None) -> Request:
    headers = [] if content_length is None else [(b"content-length", content_length.encode())]
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/test", "headers": headers}, receive)


def test_analysis_job_request_is_typed_and_rejects_unknown_job_types():
    parsed = asyncio.run(read_typed_json_object(
        _request(b'{"type":"carbon","payload":{"aoi":{"west":1}}}'), AnalysisJobRequest
    ))
    assert parsed == {"type": "carbon", "payload": {"aoi": {"west": 1}}}
    with pytest.raises(RequestValidationError):
        asyncio.run(read_typed_json_object(
            _request(b'{"type":"arbitrary","payload":{"aoi":{}}}'), AnalysisJobRequest
        ))


def test_json_reader_rejects_non_finite_values_and_excessive_depth():
    with pytest.raises(HTTPException) as non_finite:
        asyncio.run(read_typed_json_object(
            _request(b'{"aoi":{},"dnbr_threshold":NaN}'), EventMapRequest
        ))
    assert non_finite.value.status_code == 422

    nested = {"value": 1}
    for _ in range(35):
        nested = {"child": nested}
    with pytest.raises(HTTPException) as deep:
        asyncio.run(read_typed_json_object(
            _request(json.dumps({"message": "x", "context": nested}).encode()), AgentRequest
        ))
    assert deep.value.status_code == 422


def test_untyped_json_reader_still_enforces_object_shape_size_and_complexity():
    assert asyncio.run(read_bounded_json_object(_request(b'{"year":2026}'))) == {"year": 2026}
    with pytest.raises(HTTPException) as array_body:
        asyncio.run(read_bounded_json_object(_request(b'[1,2,3]')))
    assert array_body.value.status_code == 400
    with pytest.raises(HTTPException) as too_large:
        asyncio.run(read_bounded_json_object(_request(b'{"data":"12345"}'), max_bytes=8))
    assert too_large.value.status_code == 413


def test_json_reader_enforces_content_size_and_import_contract():
    with pytest.raises(HTTPException) as oversized:
        asyncio.run(read_typed_json_object(_request(b"{}", content_length="100"), FireImportRequest, max_bytes=32))
    assert oversized.value.status_code == 413

    with pytest.raises(RequestValidationError):
        asyncio.run(read_typed_json_object(
            _request(b'{"content":"data","format":"xml"}'), FireImportRequest
        ))


def test_carbon_analysis_common_fields_are_typed_and_bounded():
    parsed = asyncio.run(read_typed_json_object(
        _request(b'{"aoi":{"type":"Polygon","coordinates":[]},"year":2026,"scale":30,"model_name":"carbon"}'),
        CarbonAnalysisRequest,
    ))
    assert parsed["year"] == 2026
    assert parsed["scale"] == 30

    with pytest.raises(RequestValidationError):
        asyncio.run(read_typed_json_object(
            _request(b'{"year":3026,"start_month":13}'), CarbonAnalysisRequest
        ))
    with pytest.raises(HTTPException) as non_finite:
        asyncio.run(read_typed_json_object(
            _request(b'{"scale":NaN}'), CarbonAnalysisRequest
        ))
    assert non_finite.value.status_code == 422
