"""Pydantic schemas for the dMRV project API."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_Strict):
    name: str = Field(min_length=1, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    organization_id: str | None = Field(default=None, min_length=1, max_length=36)
    activity_type: str = Field(default="ARR", max_length=32)
    jurisdiction: str | None = Field(default=None, max_length=128)
    methodology_id: str | None = Field(default=None, max_length=64)
    methodology_version: str | None = Field(default=None, max_length=32)
    start_date: dt.date | None = None
    crediting_period_years: int | None = Field(default=None, ge=1, le=100)


class ProjectUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    organization: str | None = Field(default=None, max_length=255)
    jurisdiction: str | None = Field(default=None, max_length=128)
    methodology_id: str | None = Field(default=None, max_length=64)
    methodology_version: str | None = Field(default=None, max_length=32)
    start_date: dt.date | None = None
    crediting_period_years: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _name_cannot_be_cleared(self):
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name tidak boleh dikosongkan")
        return self


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    organization: str | None
    organization_id: str | None = None
    activity_type: str
    jurisdiction: str | None
    methodology_id: str | None
    methodology_version: str | None
    start_date: dt.date | None
    crediting_period_years: int | None
    status: str
    revision: int
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    my_role: str | None = None


class MemberAdd(_Strict):
    principal_type: Literal["user", "admin"]
    username: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=32)


class MemberOut(BaseModel):
    principal_type: str
    principal_id: int
    username: str
    role: str


class BoundaryCreate(_Strict):
    geojson: dict
    reason: str = Field(min_length=1)


class BoundaryOut(BaseModel):
    id: str
    version: int
    crs: str
    area_ha: float
    area_method: str
    checksum: str
    created_by: str
    reason: str
    created_at: dt.datetime | None
    validated_geojson: dict | None = None
    # Present on creation only: count and area of overlap with other projects.
    # Never carries ids or names of those projects.
    overlap: dict | None = None


class ArrIllustrativeLineOut(BaseModel):
    key: str
    value: Decimal | None
    unit: str
    status: Literal["computed", "requires_data"]
    equation: str


class ArrIllustrativeInputOut(BaseModel):
    key: str
    value: Decimal | None


class ArrIllustrativePreviewOut(BaseModel):
    example_id: Literal["arr_fixed_area_illustrative_v1"]
    title: str
    source_type: Literal["accounting_result"]
    qualification: Literal["illustrative_only"]
    formula_id: str
    methodology_profile: None
    verification_status: Literal["not_verified"]
    issuance_status: Literal["not_issued"]
    rounding_policy: str
    area_ha: Decimal
    normalized_inputs: list[ArrIllustrativeInputOut]
    lines: list[ArrIllustrativeLineOut]
    blockers: list[str]


class FieldSummaryJobCreate(_Strict):
    kind: Literal["field_summary"] = "field_summary"
    monitoring_period_id: str | None = Field(default=None, max_length=36)


class DmrvJobOut(BaseModel):
    id: str
    project_id: str
    kind: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: dict
    result: dict | None
    error: dict | None
    attempts: int
    cancel_requested: bool
    created_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class DmrvSnapshotOut(BaseModel):
    id: str
    project_id: str
    job_id: str | None
    kind: str
    inputs: dict
    outputs: dict
    snapshot_hash: str
    created_by: str
    created_at: dt.datetime | None
    # Derived from the latest non-superseded submission linked to the snapshot;
    # it is not part of the immutable calculation output or its checksum.
    mrv_review_status: dict | None = None


class EvidencePackageCreate(_Strict):
    snapshot_id: str = Field(min_length=1, max_length=36)


class EvidencePackageOut(BaseModel):
    id: str
    project_id: str
    snapshot_id: str
    manifest: dict
    manifest_hash: str
    package_sha256: str
    missing_components: list[str]
    readiness: Literal["incomplete", "ready"]
    created_by: str
    created_at: dt.datetime | None


class EvidencePackageVerificationOut(BaseModel):
    package_id: str
    valid: bool
    code: str | None
    checked_entries: int


class SubmissionCreate(_Strict):
    evidence_package_id: str = Field(min_length=1, max_length=36)
    supersedes_id: str | None = Field(default=None, max_length=36)


class SubmissionOut(BaseModel):
    id: str
    project_id: str
    version: int
    evidence_package_id: str
    locked_snapshot_hash: str
    locked_manifest_hash: str
    status: Literal["draft", "submitted", "under_review", "changes_requested", "approved", "rejected", "superseded"]
    created_by: str
    reviewer_key: str | None
    supersedes_id: str | None
    revision: int
    decision_reason: str | None
    created_at: dt.datetime | None
    submitted_at: dt.datetime | None
    decided_at: dt.datetime | None


class ReviewFindingCreate(_Strict):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    due_date: dt.date | None = None


class ReviewFindingResponse(_Strict):
    response: str = Field(min_length=1, max_length=4000)
    evidence_references: list[str] = Field(default_factory=list, max_length=20)


class ReviewFindingOut(BaseModel):
    id: str
    submission_id: str
    code: str
    message: str
    status: Literal["open", "responded", "resolved"]
    due_date: dt.date | None
    response: str | None
    evidence_references: list[str]
    created_by: str
    responded_by: str | None
    resolved_by: str | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None


class SubmissionDecision(_Strict):
    decision: Literal["approved", "changes_requested", "rejected"]
    reason: str = Field(min_length=1, max_length=4000)


class MonitoringPeriodCreate(_Strict):
    name: str = Field(min_length=1, max_length=128)
    start_date: dt.date
    end_date: dt.date
    boundary_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _valid_date_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date harus sama dengan atau setelah start_date")
        return self


class MonitoringPeriodUpdate(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    boundary_version: int | None = Field(default=None, ge=1)
    status: Literal["in_progress", "completed"] | None = None

    @model_validator(mode="after")
    def _date_pair_is_ordered(self):
        if self.start_date is not None and self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date harus sama dengan atau setelah start_date")
        return self


class MonitoringPeriodOut(BaseModel):
    id: str
    project_id: str
    boundary_version: int
    boundary_checksum: str
    name: str
    start_date: dt.date
    end_date: dt.date
    status: Literal["planned", "in_progress", "completed"]
    revision: int
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
