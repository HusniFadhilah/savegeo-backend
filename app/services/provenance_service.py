"""Creation and lifecycle updates for analysis provenance records."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.temporal import format_rfc3339, parse_calendar_date, parse_rfc3339
from app.core.version import get_product_version, get_public_version_metadata
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


def _normalize_acquisition(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return parse_calendar_date(value, field="acquisition_date").isoformat()
        except ValueError:
            return format_rfc3339(parse_rfc3339(value, field="acquisition_date"), field="acquisition_date")
    raise ValueError("acquisition_date must be YYYY-MM-DD or an RFC 3339 timestamp")


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
        acquisition_date=_normalize_acquisition(_value(payload, "acquisition_date", "acquisitionDate", "date", "start_date")),
        parameters=_safe_parameters(payload),
        aoi_checksum=_aoi_checksum(payload),
        crs=_value(payload, "crs", "CRS"),
        resolution=_value(payload, "resolution", "scale"),
        cloud_mask=_value(payload, "cloud_mask", "cloudMask", "cloud_masking"),
        model_id=_value(payload, "model_id", "modelId", "model"),
        model_version=_value(payload, "model_version", "modelVersion"),
        provenance={"job_id": analysis_id, "application_version": get_product_version(), "created_at": format_rfc3339(now)},
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


def _record_analysis_execution(
    db: Session,
    analysis_id: str,
    result: dict[str, Any],
    pinned_model: dict[str, Any] | None = None,
    serving_revision: dict[str, Any] | None = None,
) -> None:
    """Persist the model identity and quality signals observed in the worker result."""
    record = db.get(AnalysisProvenance, analysis_id)
    if record is None:
        logger.warning("provenance record missing analysis_id=%s", analysis_id)
        return

    model_info: dict[str, Any] = {}
    quality: dict[str, Any] = {}
    confidence: dict[str, Any] = {}
    if isinstance(result.get("model_info"), dict):
        model_info = result["model_info"]
    if isinstance(result.get("data_quality"), dict):
        quality = result["data_quality"]
    if isinstance(result.get("confidence"), dict):
        confidence = result["confidence"]
    pools = result.get("pools")
    if isinstance(pools, dict):
        model_info = {
            str(pool): value.get("model_info")
            for pool, value in pools.items()
            if isinstance(value, dict) and isinstance(value.get("model_info"), dict)
        }
        quality = {
            str(pool): value.get("data_quality")
            for pool, value in pools.items()
            if isinstance(value, dict) and isinstance(value.get("data_quality"), dict)
        }
    if isinstance(result.get("quality_assessment"), dict):
        quality = {**quality, "general_preflight": result["quality_assessment"]}

    model_name = model_info.get("model_name") if isinstance(model_info, dict) else None
    model_version = model_info.get("model_version") if isinstance(model_info, dict) else None
    if pinned_model:
        model_name = pinned_model.get("name") or model_name
        model_version = pinned_model.get("version") or model_version
    record.model_id = str(pinned_model.get("id")) if pinned_model else (str(model_name) if model_name else record.model_id)
    record.model_version = str(model_version) if model_version is not None else record.model_version
    record.quality_flags = _safe_parameters(quality)
    record.confidence = _safe_parameters(confidence or (model_info.get("cv_metrics") if isinstance(model_info, dict) else {}))
    record.artifact_checksums = (
        {"model_sha256": pinned_model["artifact_sha256"]} if pinned_model and pinned_model.get("artifact_sha256") else None
    )
    manifest = {
        "model": _safe_parameters(model_info),
        "pinned_model": _safe_parameters(pinned_model or {}),
        "serving_revision": _safe_parameters(serving_revision or {}),
        "quality": _safe_parameters(quality),
        "application_version": get_product_version(),
        "code_revision": get_public_version_metadata().get("commit_sha"),
        "recorded_at": format_rfc3339(datetime.now(UTC)),
    }
    record.provenance = {**(record.provenance or {}), "inference_manifest": manifest}
    db.commit()


def record_analysis_execution(
    db: Session,
    analysis_id: str,
    result: dict[str, Any],
    pinned_model: dict[str, Any] | None = None,
    serving_revision: dict[str, Any] | None = None,
) -> None:
    try:
        _record_analysis_execution(db, analysis_id, result, pinned_model, serving_revision)
    except Exception:  # noqa: BLE001 - optional evidence must not discard a completed map
        db.rollback()
        logger.exception("execution manifest update failed analysis_id=%s", analysis_id)
