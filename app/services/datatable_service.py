"""Generic DataTables.net server-side processing helper.

DataTables' serverSide mode sends query params `draw`, `start`, `length`,
`search[value]` (bracket syntax, not a valid Pydantic field name - read from
`request.query_params` directly) and expects back
`{draw, recordsTotal, recordsFiltered, data}`.

`is_datatables_request()` lets a route support BOTH the old "return everything"
shape (used by a couple of existing callers, e.g. the admin overview counting
models) and the new paginated shape from the same endpoint - keyed off whether
the client actually sent DataTables' `draw` param.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from fastapi import Request
from sqlalchemy import or_
from sqlalchemy.orm import Query


def is_datatables_request(request: Request) -> bool:
    return "draw" in request.query_params


def datatables_response(
    request: Request,
    base_query: Query,
    row_mapper: Callable,
    searchable_columns: Sequence = (),
) -> dict:
    params = request.query_params
    draw = int(params.get("draw", "0") or 0)
    start = int(params.get("start", "0") or 0)
    length = int(params.get("length", "10") or 10)
    search_value = (params.get("search[value]") or "").strip()

    records_total = base_query.count()

    query = base_query
    if search_value and searchable_columns:
        like = f"%{search_value}%"
        query = query.filter(or_(*[col.ilike(like) for col in searchable_columns]))
    records_filtered = query.count()

    if length == -1:
        rows = query.offset(start).all()
    else:
        rows = query.offset(start).limit(length).all()

    return {
        "draw": draw,
        "recordsTotal": records_total,
        "recordsFiltered": records_filtered,
        "data": [row_mapper(r) for r in rows],
    }
