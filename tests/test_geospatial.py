from app.api.routes.geospatial import _safe_sql
import pytest
from fastapi import HTTPException


def test_safe_sql_adds_and_caps_limit():
    assert _safe_sql("SELECT * FROM scenes", 25).endswith("LIMIT 25")
    assert _safe_sql("SELECT * FROM scenes LIMIT 999", 25).endswith("LIMIT 25")


@pytest.mark.parametrize("sql", ["DELETE FROM scenes", "SELECT * FROM scenes; DROP TABLE scenes", "PRAGMA database_list"])
def test_safe_sql_rejects_mutation(sql):
    with pytest.raises(HTTPException):
        _safe_sql(sql, 10)

