"""Cloud-native dataset catalog and safe server fallback contracts.

Browser-first processing remains the default. These endpoints deliberately
accept references and job descriptions, never credentials or large raster
payloads in URLs. Heavy execution can be attached to the existing worker
queue without changing the frontend contract.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer, require_permission
from app.core.viewer_features import (
    FEATURE_ACCESS_DENIED_DETAIL,
    ensure_feature_access,
    feature_code_for_module,
    require_any_feature,
)
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from app.core.temporal import format_rfc3339, utc_now
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
_FORBIDDEN_SQL = re.compile(
    r"(?:;|--|/\*|\*/|\b(?:COPY|ATTACH|DETACH|INSTALL|LOAD|EXPORT|IMPORT|CREATE|DROP|ALTER|INSERT|UPDATE|DELETE|PRAGMA)\b)",
    re.IGNORECASE,
)
_catalog: dict[str, dict[str, Any]] = {}
_jobs: dict[str, dict[str, Any]] = {}


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
        if value is not None and (value[0] > value[2] or value[1] > value[3]):
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


def _safe_sql(sql: str, limit: int) -> str:
    normalized = sql.strip()
    if _FORBIDDEN_SQL.search(normalized) or not re.match(r"^(SELECT|WITH)\b", normalized, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Only read-only SELECT queries are allowed")
    if re.search(r"\bLIMIT\s+\d+\b", normalized, re.IGNORECASE):
        return re.sub(r"\bLIMIT\s+\d+\b", f"LIMIT {limit}", normalized, flags=re.IGNORECASE)
    return f"{normalized} LIMIT {limit}"


def _new_job(kind: str, request: ExportRequest | QueryRequest, feature_code: str) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "mode": "server",
        "featureCode": feature_code,
        "createdAt": format_rfc3339(utc_now()),
        "request": request.model_dump(exclude_none=True),
    }
    _jobs[job_id] = job
    return job


@router.get("/datasets")
def list_cloud_datasets(
    module: str | None = None,
    format: str | None = Query(default=None),
    search: str | None = None,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    values = list(_catalog.values())
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
    dataset = _catalog.get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    return dataset


@router.post("/datasets/register", status_code=status.HTTP_201_CREATED)
def register_cloud_dataset(reference: DatasetReference, _admin: object = Depends(require_permission("geospatial.write"))):
    data = reference.model_dump(exclude_none=True)
    if reference.access in {"private", "signed"} and (reference.url or reference.assetUrl):
        raise HTTPException(
            status_code=400, detail="Private dataset URLs must be issued by the access endpoint"
        )
    _catalog[reference.id] = data
    return data


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cloud_dataset(dataset_id: str, _admin: object = Depends(require_permission("geospatial.write"))):
    if _catalog.pop(dataset_id, None) is None:
        raise HTTPException(status_code=404, detail="Dataset not found")


@router.get("/datasets/{dataset_id}/access")
def dataset_access(
    dataset_id: str,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    dataset = _catalog.get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
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
    dataset = _catalog.get(request.datasetId)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    feature_code = _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    return _new_job("cog", request, feature_code)


@router.post("/export/geoparquet", status_code=status.HTTP_202_ACCEPTED)
def export_geoparquet(
    request: ExportRequest,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    dataset = _catalog.get(request.datasetId)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    feature_code = _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    return _new_job("geoparquet", request, feature_code)


@router.post("/query", status_code=status.HTTP_202_ACCEPTED)
def geospatial_query(
    request: QueryRequest,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    dataset = _catalog.get(request.datasetId)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    feature_code = _ensure_dataset_feature_access(viewer, dataset.get("module"), db)
    safe_sql = _safe_sql(request.sql, request.limit)
    job = _new_job("query", request, feature_code)
    job["request"]["sql"] = safe_sql
    return job


@router.get("/jobs/{job_id}")
def get_geospatial_job(
    job_id: str,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ensure_feature_access(viewer, job.get("featureCode"), db)
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_geospatial_job(
    job_id: str,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    ensure_feature_access(viewer, job.get("featureCode"), db)
    job["status"] = "cancelled"
    return job
