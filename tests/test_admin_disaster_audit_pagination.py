import inspect
from types import SimpleNamespace

from app.api.routes import admin_disaster


class AuditQuery:
    def __init__(self, rows):
        self.rows = rows
        self.offset_value = None
        self.limit_value = None

    def filter_by(self, **filters):
        assert filters == {"resource_type": "disaster_event", "resource_id": "17"}
        return self

    def with_entities(self, *_columns):
        return self

    def scalar(self):
        return 123

    def order_by(self, *_columns):
        return self

    def offset(self, value):
        self.offset_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def all(self):
        return self.rows


def test_event_audit_returns_bounded_page_and_total(monkeypatch):
    rows = [SimpleNamespace(to_dict=lambda: {"id": 41})]
    query = AuditQuery(rows)

    class DB:
        def query(self, model):
            assert model is admin_disaster.AuditLog
            return query

    monkeypatch.setattr(admin_disaster, "_get_event_or_404", lambda _db, _event_id: object())
    result = admin_disaster.admin_event_audit(
        id=17,
        page=4,
        page_size=20,
        admin=SimpleNamespace(id=1),
        db=DB(),
    )

    assert result == {"logs": [{"id": 41}], "page": 4, "page_size": 20, "total": 123}
    assert query.offset_value == 60
    assert query.limit_value == 20


def test_event_audit_page_size_has_conservative_default_and_maximum():
    page_size = inspect.signature(admin_disaster.admin_event_audit).parameters["page_size"].default
    assert page_size.default == 50
    assert any(getattr(constraint, "le", None) == 100 for constraint in page_size.metadata)
