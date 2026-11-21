from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.api.routes import admin


def test_legacy_model_catalog_uses_bound_filters_and_allowlisted_columns():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE uploaded_models (id INTEGER PRIMARY KEY, name TEXT, model_type TEXT, "
            "is_active BOOLEAN, uploaded_at TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO uploaded_models (id, name, model_type, is_active, uploaded_at) "
            "VALUES (1, 'carbon-a', 'carbon', 1, '2026-01-01'), (2, 'landcover-a', 'landcover', 1, '2026-01-02')"
        ))

    factory = sessionmaker(bind=engine)
    request = Request({"type": "http", "method": "GET", "path": "/api/admin/models", "headers": [], "query_string": b""})
    try:
        with factory() as db:
            result = admin.admin_models_list(
                request, model_type="carbon' OR 1=1 --", admin=SimpleNamespace(id=1), db=db,
            )
        assert result == {"models": []}
    finally:
        engine.dispose()
