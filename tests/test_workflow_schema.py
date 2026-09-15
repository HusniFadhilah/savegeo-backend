from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.workflow import WorkflowDefinition


def _workflow(**overrides):
    value = {
        "name": "NDVI",
        "category": "vegetation",
        "visibility": "private",
        "nodes": [
            {"id": "load", "type": "load_aoi", "label": "AOI", "position": {"x": 0, "y": 0}},
            {"id": "ndvi", "type": "ndvi", "label": "NDVI", "position": {"x": 1, "y": 0}},
        ],
        "edges": [{"id": "e1", "source": "load", "target": "ndvi"}],
    }
    value.update(overrides)
    return value


def test_workflow_schema_rejects_cycles():
    with pytest.raises(ValidationError, match="circular"):
        WorkflowDefinition.model_validate(_workflow(edges=[{"id": "e1", "source": "load", "target": "ndvi"}, {"id": "e2", "source": "ndvi", "target": "load"}]))


def test_workflow_schema_rejects_unknown_edge_node():
    with pytest.raises(ValidationError, match="unknown node"):
        WorkflowDefinition.model_validate(_workflow(edges=[{"id": "e1", "source": "load", "target": "missing"}]))


def test_workflow_schema_is_versioned():
    result = WorkflowDefinition.model_validate(_workflow())
    assert result.version == 1
    assert result.schemaVersion == 1
