import pytest
from pydantic import ValidationError

from app.api.routes.geospatial import QueryRequest


@pytest.mark.parametrize("limit", [0, -1, 10001])
def test_query_contract_rejects_unbounded_result_limits(limit):
    with pytest.raises(ValidationError):
        QueryRequest(datasetId="scene", sql="SELECT * FROM scenes", limit=limit)


def test_query_contract_rejects_oversized_sql_before_processing():
    with pytest.raises(ValidationError):
        QueryRequest(datasetId="scene", sql="SELECT " + "x" * 50000)
