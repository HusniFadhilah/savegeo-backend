"""Strict, finite, bounded inputs for the seven integrity workflows."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class EvidenceInput(Strict):
    summary: str = Field(min_length=1, max_length=8000)
    evidence: list[str] = Field(default_factory=list, max_length=30)
    due_date: dt.date | None = None
    owner: str = Field(default="", max_length=100)


class PoolStock(Strict):
    pool: Literal["AGB", "BGB", "SOC", "litter", "deadwood"]
    project_start: float = Field(ge=0, le=1e7)
    project_end: float = Field(ge=0, le=1e7)
    baseline_start: float = Field(ge=0, le=1e7)
    baseline_end: float = Field(ge=0, le=1e7)


class AccountingInput(Strict):
    period_id: str = Field(min_length=1, max_length=36)
    methodology_id: str = Field(min_length=1, max_length=64)
    methodology_version: str = Field(min_length=1, max_length=32)
    profile_reference: HttpUrl
    rule_justification: str = Field(min_length=10, max_length=8000)
    pools: list[PoolStock] = Field(min_length=1, max_length=5)
    excluded_pools_reason: str = Field(default="", max_length=4000)
    project_emissions_tco2e: float | None = Field(default=None, ge=0, le=1e12)
    leakage_tco2e: float | None = Field(default=None, ge=0, le=1e12)
    uncertainty_deduction_pct: float | None = Field(default=None, ge=0, le=100)
    buffer_pct: float | None = Field(default=None, ge=0, le=100)
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_pools(self):
        if len({p.pool for p in self.pools}) != len(self.pools):
            raise ValueError("Pool tidak boleh berulang.")
        if len(self.pools) < 5 and not self.excluded_pools_reason:
            raise ValueError("Jelaskan alasan pool lain dikecualikan.")
        return self


class RegistryInput(Strict):
    registry: str = Field(min_length=1, max_length=64)
    external_project_id: str = Field(min_length=1, max_length=100)
    registry_url: HttpUrl
    vintage_start: dt.date
    vintage_end: dt.date
    serial_prefix: str = Field(min_length=1, max_length=100)
    serial_start: int = Field(ge=0, le=9007199254740991)
    serial_end: int = Field(ge=0, le=9007199254740991)
    unit_status: Literal["reported_issued", "reported_retired", "reported_cancelled"]
    checked_on: dt.date
    evidence: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def ordered(self):
        if self.vintage_end < self.vintage_start or self.serial_end < self.serial_start:
            raise ValueError("Rentang tanggal atau nomor seri terbalik.")
        if self.checked_on > dt.datetime.now(dt.UTC).date():
            raise ValueError("Tanggal pemeriksaan tidak boleh di masa depan.")
        return self


class FieldPair(Strict):
    point_id: str = Field(min_length=1, max_length=100)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-85, le=85)
    observed: float = Field(ge=0, le=1e7)
    predicted: float = Field(ge=0, le=1e7)


class ValidationInput(Strict):
    pool: Literal["AGB", "BGB", "SOC", "litter", "deadwood"]
    product: str = Field(min_length=1, max_length=200)
    reference_year: int = Field(ge=1900, le=2100)
    predictor_year: int = Field(ge=1900, le=2100)
    label_source: Literal["field_measurements", "reference_map"]
    unit: Literal["Mg_C_ha", "Mg_dry_matter_ha"]
    provenance: str = Field(min_length=10, max_length=8000)
    block_degrees: float = Field(default=0.1, ge=0.01, le=5)
    buffer_km: float = Field(default=10, ge=0, le=100)
    pairs: list[FieldPair] = Field(min_length=3, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_points(self):
        if len({p.point_id for p in self.pairs}) != len(self.pairs):
            raise ValueError("point_id harus unik.")
        if len({(p.lon, p.lat) for p in self.pairs}) != len(self.pairs):
            raise ValueError("Koordinat berulang harus diagregasi sebelum validasi.")
        return self


class RiskInput(EvidenceInput):
    signal: Literal["tree_cover_change", "fire_detection", "field_report", "data_gap", "other"]
    severity: Literal["low", "medium", "high"]
    observed_on: dt.date
    affected_area_ha: float | None = Field(default=None, ge=0, le=500000)


class GrievanceInput(EvidenceInput):
    topic: Literal["consent", "benefit_sharing", "access", "other"]
    severity: Literal["low", "medium", "high"]
    received_on: dt.date


class RecordSave(Strict):
    title: str = Field(min_length=1, max_length=200)
    payload: dict
    status: Literal["draft", "submitted", "open"] = "draft"


class RecordReview(Strict):
    decision: Literal["accepted", "rejected", "not_applicable", "acknowledged", "resolved"]
    reason: str = Field(min_length=5, max_length=4000)


class RiskPolicyInput(Strict):
    enabled: bool = False
    interval_days: int = Field(default=7, ge=1, le=90)
    lookback_days: int = Field(default=30, ge=7, le=90)
    tree_loss_threshold_ha: float = Field(default=1, gt=0, le=500000)
    min_observations: int = Field(default=3, ge=1, le=30)


class RiskScanInput(Strict):
    end_date: dt.date | None = None


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    category: str
    record_key: str
    title: str
    payload: dict
    result: dict
    status: str
    revision: int
    created_by: str
    updated_by: str
    reviewed_by: str | None
    review_reason: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
