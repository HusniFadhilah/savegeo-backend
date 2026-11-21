"""Cloud-native dataset catalog and safe server fallback contracts.

Browser-first processing remains the default. These endpoints deliberately
accept references and job descriptions, never credentials or large raster
payloads in URLs. Heavy execution can be attached to the existing worker
queue without changing the frontend contract.
"""

from __future__ import annotations

import ipaddress
import math
import re
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from app.core.security import get_current_app_viewer, require_permission
from app.core.viewer_features import (
    FEATURE_ACCESS_DENIED_DETAIL,
    ensure_feature_access,
    feature_code_for_module,
    require_any_feature,
)
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.models.geospatial_dataset import GeospatialDataset
from app.db.session import get_db
from app.schemas.metadata import DatasetMetadata

router = APIRouter(
    prefix="/geospatial",
    tags=["geospatial"],
    dependencies=[Depends(require_any_feature())],
)

def _ensure_dataset_feature_access(
    principal: AdminUser | User,
    module: str | None,
    db: Session,
) -> str:
    code = feature_code_for_module(module)
    if code is None:
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
    ensure_feature_access(principal, code, db)
    return code


def _dataset_feature_allowed(principal: AdminUser | User, module: str | None, db: Session) -> bool:
    try:
        _ensure_dataset_feature_access(principal, module, db)
        return True
    except HTTPException as exc:
        if exc.status_code == 403:
            return False
        raise

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_URL = re.compile(r"^https?://", re.IGNORECASE)
def _safe_remote_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname
    if parsed.username or parsed.password or not host:
        raise ValueError("dataset URLs must not contain credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    ):
        raise ValueError("dataset URL host must not be a private or local address")
    if host.casefold() in {"localhost", "localhost.localdomain"} or host.casefold().endswith(".local"):
        raise ValueError("dataset URL host must not be local")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("dataset URL contains an invalid port") from exc
    return value


class DatasetReference(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=255)
    format: Literal["cog", "geoparquet", "pmtiles"]
    url: str | None = None
    assetUrl: str | None = None
    metadataUrl: str | None = None
    mimeType: str | None = None
    sizeBytes: int | None = Field(default=None, ge=0)
    checksum: str | None = None
    crs: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    bands: list[dict[str, Any]] | None = None
    timeExtent: dict[str, str | None] | None = None
    access: Literal["public", "private", "signed"] = "public"
    sourceType: Literal["local-file", "browser-cache", "backend", "remote"]
    version: str | None = None
    module: str | None = None
    metadata: DatasetMetadata | None = None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid dataset id")
        return value

    @field_validator("url", "assetUrl", "metadataUrl")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is not None and not _URL.match(value):
            raise ValueError("only http(s) dataset URLs are allowed")
        return _safe_remote_url(value) if value is not None else None

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and (not all(math.isfinite(n) for n in value) or not (-180 <= value[0] <= value[2] <= 180 and -90 <= value[1] <= value[3] <= 90)):
            raise ValueError("invalid bbox")
        return value


class ExportRequest(BaseModel):
    datasetId: str = Field(min_length=1, max_length=128)
    operation: Literal["export"] = "export"
    runReference: str | None = Field(default=None, max_length=128)
    estimatedSizeBytes: int | None = Field(default=None, ge=0)
    mode: Literal["auto", "server"] = "auto"


class QueryRequest(BaseModel):
    datasetId: str = Field(min_length=1, max_length=128)
    sql: str = Field(min_length=1, max_length=50_000)
    params: list[Any] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=1000, ge=1, le=10_000)


@router.get("/datasets")
def list_cloud_datasets(
    module: str | None = Query(default=None, max_length=64),
    format: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    rows = db.query(GeospatialDataset).order_by(GeospatialDataset.id).all()
    values = [row.document for row in rows]
    if module is not None:
        _ensure_dataset_feature_access(viewer, module, db)
        values = [item for item in values if item.get("module") == module]
    else:
        values = [item for item in values if _dataset_feature_allowed(viewer, item.get("module"), db)]
    if format:
        values = [item for item in values if item.get("format") == format]
    if search:
        term = search.lower()
        values = [
            item
            for item in values
            if term in item.get("name", "").lower() or term in item.get("id", "").lower()
        ]
    return {"datasets": values, "count": len(values)}


@router.get("/datasets/{dataset_id}")
def get_cloud_dataset(
    dataset_id: str,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    row = db.get(GeospatialDataset, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset = row.document
    _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    return dataset


@router.post("/datasets/register", status_code=status.HTTP_201_CREATED)
def register_cloud_dataset(
    reference: DatasetReference,
    admin: object = Depends(require_permission("geospatial.write")),
    db: Session = Depends(get_db),
):
    data = reference.model_dump(exclude_none=True)
    if reference.access in {"private", "signed"} and (reference.url or reference.assetUrl):
        raise HTTPException(
            status_code=400, detail="Private dataset URLs must be issued by the access endpoint"
        )
    if db.bind.dialect.name == "postgresql":
        # Serialize capacity checks across API workers. The primary key below
        # still handles duplicate registration races safely.
        db.execute(text("SELECT pg_advisory_xact_lock(729401702)"))
    if db.get(GeospatialDataset, reference.id) is not None:
        raise HTTPException(status_code=409, detail="Dataset already registered")
    if db.query(func.count(GeospatialDataset.id)).scalar() >= 2000:
        raise HTTPException(status_code=503, detail="Dataset catalog capacity reached")
    admin_id = getattr(admin, "id", None)
    db.add(GeospatialDataset(id=reference.id, document=data, created_by=f"admin:{admin_id or 'unknown'}"))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Dataset already registered") from exc
    return data


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cloud_dataset(
    dataset_id: str,
    _admin: object = Depends(require_permission("geospatial.write")),
    db: Session = Depends(get_db),
):
    row = db.get(GeospatialDataset, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    db.delete(row)
    db.commit()


@router.get("/datasets/{dataset_id}/access")
def dataset_access(
    dataset_id: str,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    row = db.get(GeospatialDataset, dataset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset = row.document
    _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    if dataset.get("access") != "public":
        raise HTTPException(status_code=501, detail="Signed access provider is not configured")
    return {"datasetId": dataset_id, "url": dataset.get("url") or dataset.get("assetUrl"), "expiresAt": None}


@router.post("/export/cog", status_code=status.HTTP_202_ACCEPTED)
def export_cog(
    request: ExportRequest,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    row = db.get(GeospatialDataset, request.datasetId)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset = row.document
    _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    raise HTTPException(501, "Server COG export is unavailable; use browser processing")


@router.post("/export/geoparquet", status_code=status.HTTP_202_ACCEPTED)
def export_geoparquet(
    request: ExportRequest,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    row = db.get(GeospatialDataset, request.datasetId)
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset = row.document
    _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    raise HTTPException(501, "Server GeoParquet export is unavailable; use browser processing")


@router.post("/query", status_code=status.HTTP_202_ACCEPTED)
def geospatial_query(
    request: QueryRequest,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    # There is no server SQL executor wired to this contract. Never accept a
    # misleading queued job or pass user SQL to the application DB. Browser
    # DuckDB remains available; enabling a server executor requires a separate
    # read-only role, structural validation and a real durable worker.
    raise HTTPException(status_code=501, detail="Server Spatial SQL is unavailable; use browser processing")

