"""Common metadata contract for geospatial datasets and analysis exports."""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

_DATASET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHECKSUM = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")


class BandMetadata(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    units: str = Field(min_length=1, max_length=64)
    nodata: float | int | str | None = None


class DatasetMetadata(BaseModel):
    """Minimum metadata needed to publish a reproducible data product."""

    dataset_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    abstract: str = Field(min_length=1, max_length=5000)
    owner: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=1, max_length=2000)
    license: str = Field(min_length=1, max_length=500)
    attribution: str = Field(min_length=1, max_length=2000)
    temporal_start: date | datetime
    temporal_end: date | datetime
    bbox: list[float] = Field(min_length=4, max_length=4)
    crs: str = Field(min_length=1, max_length=128)
    resolution: float = Field(gt=0)
    scale: float = Field(gt=0)
    units: str = Field(min_length=1, max_length=64)
    nodata: float | int | str | None
    bands: list[BandMetadata] = Field(min_length=1)
    acquisition_date: date | datetime
    processing_date: date | datetime
    processing_level: str = Field(min_length=1, max_length=64)
    cloud_cover_percent: float | None = Field(ge=0, le=100)
    lineage: list[str] = Field(min_length=1)
    processing_software: str = Field(min_length=1, max_length=255)
    model_version: str | None
    quality_statement: str = Field(min_length=1, max_length=5000)
    known_limitations: list[str] = Field(min_length=1)
    contact: str = Field(min_length=1, max_length=255)
    checksum: str
    retention_policy: str = Field(min_length=1, max_length=255)

    @field_validator("dataset_id")
    @classmethod
    def valid_dataset_id(cls, value: str) -> str:
        if not _DATASET_ID.fullmatch(value):
            raise ValueError("dataset_id must use a stable identifier format")
        return value

    @field_validator("source_url")
    @classmethod
    def valid_source_url(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("source_url must use http(s)")
        return value

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[float]) -> list[float]:
        west, south, east, north = value
        if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("bbox coordinates are outside WGS84 bounds")
        if west > east or south > north:
            raise ValueError("bbox ordering is invalid")
        return value

    @field_validator("checksum")
    @classmethod
    def valid_checksum(cls, value: str) -> str:
        if not _CHECKSUM.fullmatch(value):
            raise ValueError("checksum must be a SHA-256 hex digest")
        return value.lower()

    @model_validator(mode="after")
    def valid_dates(self) -> "DatasetMetadata":
        if self.temporal_start > self.temporal_end:
            raise ValueError("temporal_start must not be after temporal_end")
        if self.processing_date < self.acquisition_date:
            raise ValueError("processing_date must not be before acquisition_date")
        return self
