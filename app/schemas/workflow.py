"""Strict request/response validation for workflow persistence and runs."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Visibility = Literal["private", "unlisted", "public"]
Category = Literal["carbon", "vegetation", "crop", "land-cover", "disaster", "general"]


class Position(BaseModel):
    x: float
    y: float


class WorkflowNode(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    type: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    position: Position
    params: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    source: str
    target: str
    sourcePort: str | None = None
    targetPort: str | None = None


class WorkflowInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    type: Literal["aoi", "raster", "imagery", "dem", "date", "year", "threshold", "model", "layer"]
    label: str = Field(min_length=1, max_length=160)
    required: bool = True
    value: Any = None


class WorkflowMetadata(BaseModel):
    authorId: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    applicationVersion: str | None = None
    crs: str | None = None
    datasetReferences: list[str] = Field(default_factory=list)
    pluginVersions: dict[str, str] = Field(default_factory=dict)


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    schemaVersion: int = Field(default=1, ge=1, le=1)
    id: str | None = None
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: Category = "general"
    visibility: Visibility = "private"
    inputs: list[WorkflowInput] = Field(default_factory=list, max_length=100)
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=200)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=400)
    metadata: WorkflowMetadata | None = None

    @field_validator("nodes")
    @classmethod
    def unique_node_ids(cls, value: list[WorkflowNode]) -> list[WorkflowNode]:
        ids = [node.id for node in value]
        if len(ids) != len(set(ids)):
            raise ValueError("WORKFLOW_INVALID: node ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowDefinition":
        node_ids = {node.id for node in self.nodes}
        for edge in self.edges:
            if edge.source not in node_ids or edge.target not in node_ids:
                raise ValueError("WORKFLOW_INVALID: edge references an unknown node")
            if edge.source == edge.target:
                raise ValueError("WORKFLOW_INVALID: self-referencing edges are not allowed")

        incoming = {node_id: 0 for node_id in node_ids}
        adjacency = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            incoming[edge.target] += 1
            adjacency[edge.source].append(edge.target)
        queue = [node_id for node_id, count in incoming.items() if count == 0]
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for target in adjacency[current]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    queue.append(target)
        if visited != len(node_ids):
            raise ValueError("WORKFLOW_INVALID: circular dependency detected")
        return self


class WorkflowCreateRequest(BaseModel):
    workflow: WorkflowDefinition


class WorkflowUpdateRequest(BaseModel):
    workflow: WorkflowDefinition


class WorkflowRunRequest(BaseModel):
    executionLocation: Literal["browser", "backend", "auto"] = "auto"

