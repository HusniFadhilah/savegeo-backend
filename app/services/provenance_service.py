"""Creation and lifecycle updates for analysis provenance records."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.temporal import format_rfc3339
from app.db.models.analysis_provenance import AnalysisProvenance

logger = logging.getLogger(__name__)
_SECRET_WORDS = ("password", "secret", "token", "api_key", "credential", "private_key")


def _safe_parameters(value: Any, key: str = "") -> Any:
    if any(word in key.casefold() for word in _SECRET_WORDS):
        return "[REDACTED]"
    if key.casefold() in {"aoi", "geojson", "geometry", "features"}:
        return "[AOI_EXCLUDED; checksum stored separately]"
    if isinstance(value, dict):
        return {str(k): _safe_parameters(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_parameters(item, key) for item in value]
    return value


def _aoi_checksum(payload: dict[str, Any]) -> str | None:
    aoi = next(
        (payload.get(key) for key in ("aoi", "geojson", "aoi_geojson", "aoiGeojson") if payload.get(key)),
        None,
    )
    if aoi is None:
        return None
    canonical = json.dumps(aoi, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return None


def create_analysis_provenance(
    db: Session,
    analysis_id: str,
    analysis_type: str,
    payload: dict[str, Any],
    user_id: int | None = None,
) -> AnalysisProvenance:
    now = datetime.now(UTC)
    dataset = _value(payload, "dataset_id", "datasetId", "dataset")
    if isinstance(dataset, dict):
        dataset = dataset.get("id") or dataset.get("key")
    record = AnalysisProvenance(
        id=analysis_id,
        analysis_type=analysis_type,
        status="queued",
        dataset_id=str(dataset) if dataset is not None else None,
        dataset_version=_value(payload, "dataset_version", "datasetVersion", "year"),
        acquisition_date=_value(payload, "acquisition_date", "acquisitionDate", "date", "start_date"),
        parameters=_safe_parameters(payload),
        aoi_checksum=_aoi_checksum(payload),
        crs=_value(payload, "crs", "CRS"),
        resolution=_value(payload, "resolution", "scale"),
        cloud_mask=_value(payload, "cloud_mask", "cloudMask", "cloud_masking"),
        model_id=_value(payload, "model_id", "modelId", "model"),
        model_version=_value(payload, "model_version", "modelVersion"),
        provenance={"job_id": analysis_id, "application_version": "0.1.0", "created_at": format_rfc3339(now)},
        created_by_user_id=user_id,
        created_at=now,
    )
    db.add(record)
    db.commit()
    return record


def update_analysis_provenance(db: Session, analysis_id: str, **updates: Any) -> None:
    try:
        record = db.get(AnalysisProvenance, analysis_id)
        if record is None:
            logger.warning("provenance record missing analysis_id=%s", analysis_id)
            return
        for key, value in updates.items():
            if hasattr(record, key):
                setattr(record, key, value)
        db.commit()
    except Exception:  # noqa: BLE001 - job result must not be lost due to evidence write failure
        db.rollback()
        logger.exception("provenance update failed analysis_id=%s", analysis_id)
