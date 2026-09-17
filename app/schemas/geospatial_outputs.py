"""RFC 7946, TileJSON 3.0 and output-registry contracts."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.temporal import format_rfc3339, parse_rfc3339


class GeoJSONFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[dict[str, Any]] = Field(default_factory=list)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=6)

    @field_validator("features")
    @classmethod
    def validate_features(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for feature in value:
            if feature.get("type") != "Feature":
                raise ValueError("every GeoJSON member must be a Feature")
            if not isinstance(feature.get("properties", {}), dict):
                raise ValueError("GeoJSON Feature properties must be an object")
            if "crs" in feature:
                raise ValueError("RFC 7946 GeoJSON must not use the legacy crs member")
        return value


class TileJSONDocument(BaseModel):
    tilejson: Literal["3.0.0"] = "3.0.0"
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    version: str = Field(min_length=1, max_length=64)
    scheme: Literal["xyz", "tms"] = "xyz"
    tiles: list[str] = Field(min_length=1)
    minzoom: int = Field(default=0, ge=0, le=30)
    maxzoom: int = Field(default=22, ge=0, le=30)
    bounds: list[float] = Field(min_length=4, max_length=4)
    center: list[float] = Field(min_length=3, max_length=3)
    attribution: str = Field(min_length=1, max_length=1000)
    created_at: dt.datetime
    vector_layers: list[dict[str, Any]] | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, value: object):
        return parse_rfc3339(value, field="created_at")

    @field_validator("bounds")
    @classmethod
    def validate_bounds(cls, value: list[float]) -> list[float]:
        west, south, east, north = value
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError("TileJSON bounds must be [west,south,east,north] in WGS84")
        return value

    @field_validator("tiles")
    @classmethod
    def validate_tiles(cls, value: list[str]) -> list[str]:
        if any(not (item.startswith("/") or item.startswith("https://") or item.startswith("http://")) for item in value):
            raise ValueError("TileJSON tile URLs must be absolute http(s) URLs or documented internal paths")
        return value

    @field_validator("center")
    @classmethod
    def validate_center(cls, value: list[float]) -> list[float]:
        lon, lat, _zoom = value
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("TileJSON center must use longitude, latitude")
        return value

    @model_validator(mode="after")
    def validate_zoom(self) -> "TileJSONDocument":
        if self.minzoom > self.maxzoom:
            raise ValueError("minzoom must not exceed maxzoom")
        return self

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        data["created_at"] = format_rfc3339(self.created_at, field="created_at")
        return data


class OutputDescriptor(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    role: Literal["data", "visualization", "metadata", "provenance"]
    media_type: str = Field(min_length=1, max_length=128)
    href: str = Field(min_length=1, max_length=2000)
    profile: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = None
    expires_at: dt.datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("expires_at", mode="before")
    @classmethod
    def parse_expiry(cls, value: object):
        return None if value is None else parse_rfc3339(value, field="expires_at")

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if self.expires_at is not None:
            data["expires_at"] = format_rfc3339(self.expires_at, field="expires_at")
        return data


class GeospatialActionResponse(BaseModel):
    action_id: str
    action_type: str
    status: str
    created_at: dt.datetime
    completed_at: dt.datetime | None = None
    temporal_extent: dict[str, str] | None = None
    outputs: list[OutputDescriptor] = Field(default_factory=list)

    @field_validator("created_at", "completed_at", mode="before")
    @classmethod
    def parse_action_time(cls, value: object, info):
        return None if value is None else parse_rfc3339(value, field=info.field_name)

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        data["created_at"] = format_rfc3339(self.created_at, field="created_at")
        if self.completed_at is not None:
            data["completed_at"] = format_rfc3339(self.completed_at, field="completed_at")
        return data
