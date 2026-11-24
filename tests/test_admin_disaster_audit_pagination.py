import inspect
import datetime as dt
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.api.routes import admin_disaster
from app.db.models.disaster_event import DisasterEvent
from app.repositories.disaster_repo import list_events_page


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_type, _compiler, **_kwargs):
    return "JSON"


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


def test_admin_disaster_event_list_uses_bounded_server_pagination(monkeypatch):
    events = [SimpleNamespace(to_dict=lambda: {"id": 21})]
    calls = {}

    def list_page(db, **kwargs):
        calls.update(kwargs)
        return events, 41

    monkeypatch.setattr(admin_disaster.disaster_repo, "list_events_page", list_page)
    result = admin_disaster.admin_list_events(
        page=2,
        page_size=20,
        admin=SimpleNamespace(id=1),
        db=object(),
    )

    assert result == {
        "events": [{"id": 21}],
        "page": 2,
        "page_size": 20,
        "total": 41,
        "has_more": True,
    }
    assert calls["published_only"] is False
    assert calls["page"] == 2 and calls["page_size"] == 20


def test_admin_disaster_page_size_is_bounded():
    page_size = inspect.signature(admin_disaster.admin_list_events).parameters["page_size"].default
    assert page_size.default == 25
    assert any(getattr(constraint, "le", None) == 100 for constraint in page_size.metadata)


def test_disaster_repository_page_returns_stable_bounded_rows_and_total():
    engine = create_engine("sqlite://")
    DisasterEvent.__table__.create(engine)
    try:
        with Session(engine) as db:
            db.add_all([
                DisasterEvent(name=f"Event {index}", disaster_type="forest_fire", status="draft",
                              severity="high", event_date=dt.date(2026, 1, index))
                for index in range(1, 6)
            ])
            db.commit()

            rows, total = list_events_page(
                db, page=2, page_size=2, published_only=False, status="draft", severity="high", year=2026,
            )

            assert total == 5
            assert [row.name for row in rows] == ["Event 3", "Event 2"]
    finally:
        DisasterEvent.__table__.drop(engine)
        engine.dispose()
