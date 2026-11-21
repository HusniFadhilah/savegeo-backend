from __future__ import annotations

from types import SimpleNamespace

from starlette.requests import Request

from app.api.routes import admin


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.offset_value = None
        self.limit_value = None
        self.filters = []

    def filter_by(self, **_kwargs):
        return self

    def filter(self, expression):
        self.filters.append(expression)
        return self

    def order_by(self, *_args):
        return self

    def count(self):
        return len(self.rows)

    def offset(self, value):
        self.offset_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def all(self):
        return self.rows[self.offset_value:self.offset_value + self.limit_value]


def test_admin_company_list_is_paged_and_search_is_escaped():
    rows = [SimpleNamespace(to_dict=lambda i=i: {"id": i}) for i in range(1, 4)]
    query = _Query(rows)
    request = Request({"type": "http", "method": "GET", "path": "/api/admin/companies", "headers": [], "query_string": b""})

    result = admin.admin_companies_list(
        request,
        industry_type=None,
        province=None,
        search="50%_off",
        limit=1,
        offset=1,
        admin=SimpleNamespace(id=1),
        db=SimpleNamespace(query=lambda _model: query),
    )

    assert result == {
        "companies": [{"id": 2}],
        "count": 1,
        "total": 3,
        "limit": 1,
        "offset": 1,
        "has_more": True,
    }
    assert len(query.filters) == 1


def test_overpass_import_uses_bounded_dns_pinned_transport(monkeypatch):
    captured = {}
    monkeypatch.setattr(admin, "_OVERPASS_SERVERS", ["https://overpass.example.test/api"])

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"elements": []}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(admin.public_http, "post", post)
    payload, error = admin._fetch_osm_overpass("[out:json];out;\n")

    assert payload == {"elements": []}
    assert error == ""
    assert captured["timeout"] <= 60
    assert captured["max_bytes"] == admin._OVERPASS_RESPONSE_LIMIT_BYTES
    assert captured["headers"]["User-Agent"].startswith("SaveGeo/")


def test_overpass_import_fails_fast_when_capacity_is_exhausted(monkeypatch):
    class FullSemaphore:
        def acquire(self, timeout):
            assert timeout == 5
            return False

        def release(self):
            raise AssertionError("A non-acquired semaphore must not be released")

    monkeypatch.setattr(admin, "_overpass_semaphore", FullSemaphore())
    monkeypatch.setattr(admin.public_http, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network call must not run")))
    payload, error = admin._fetch_osm_overpass("query")
    assert payload is None
    assert "capacity is busy" in error
