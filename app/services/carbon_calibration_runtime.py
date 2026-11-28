"""Fail-closed runtime selection for approved local carbon calibrations."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models.carbon_calibration import CarbonCalibrationDataset, CarbonCalibrationModel, CarbonCalibrationRun
from app.services.calibration_review import manifest
from app.services.carbon_calibration_service import sha256_file


def _not_applied(result: dict[str, Any], reason: str, model_id: Any = None) -> dict[str, Any]:
    updated = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    info = updated.setdefault("model_info", {})
    info["calibration"] = {
        "status": "not_applied",
        "requested_model_id": model_id,
        "reason": reason,
        "note": "Raster/peta tidak diubah. Kalibrasi hanya berjalan bila identitas, cakupan, dan jalur transformasi piksel dapat diverifikasi.",
    }
    return updated


def _geometry(value: Any):
    if not isinstance(value, dict):
        return None
    from shapely.geometry import shape
    from shapely.ops import unary_union

    if value.get("type") == "Feature":
        return shape(value.get("geometry") or {})
    if value.get("type") == "FeatureCollection":
        geometries = [shape(item["geometry"]) for item in value.get("features", []) if isinstance(item, dict) and isinstance(item.get("geometry"), dict)]
        return unary_union(geometries) if geometries else None
    return shape(value)


def _source_sensors(result: dict[str, Any]) -> set[str]:
    quality = result.get("data_quality") if isinstance(result.get("data_quality"), dict) else {}
    source_rows = quality.get("source_provenance") or result.get("source_provenance") or []
    values = []
    for row in source_rows if isinstance(source_rows, list) else []:
        if isinstance(row, dict):
            values.extend(str(row.get(key) or "").lower() for key in ("provider", "collection_id", "sensor"))
    sensors = set()
    joined = " ".join(values)
    if any(token in joined for token in ("sentinel-2", "sentinel_2", "copernicus/s2", "s2_sr")):
        sensors.add("sentinel-2")
    if any(token in joined for token in ("landsat", "lc08", "lc09", "le07", "lt05")):
        sensors.add("landsat")
    if any(token in joined for token in ("sentinel-1", "sentinel_1", "copernicus/s1")):
        sensors.add("sentinel-1")
    return sensors


def _compatible_base(artifact: dict[str, Any], model_pin: dict[str, Any] | None) -> bool:
    baseline = artifact.get("baseline_model")
    if not isinstance(baseline, dict) or not isinstance(model_pin, dict):
        return False
    baseline_id = str(baseline.get("id") or "")
    pin_ids = {str(model_pin.get("id") or ""), str(model_pin.get("name") or "")}
    return (
        baseline_id in pin_ids
        and str(baseline.get("version") or "") == str(model_pin.get("version") or "")
        and str(baseline.get("artifact_sha256") or "") == str(model_pin.get("artifact_sha256") or "")
        and bool(baseline.get("preprocessing_version"))
        and baseline.get("preprocessing_version") == model_pin.get("preprocessing_version")
    )


def resolve_calibration_for_raster(
    db: Session,
    payload: dict[str, Any],
    *,
    model_pin: dict[str, Any] | None,
    target_pool: str | None,
    unit: str | None,
    source_provenance: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve an explicitly selected calibration before raster statistics/tiles are made."""
    requested_id = payload.get("calibration_model_id")
    if requested_id in (None, ""):
        return None, None

    def refused(reason: str) -> tuple[None, dict[str, Any]]:
        return None, {"status": "not_applied", "requested_model_id": requested_id, "reason": reason,
                      "note": "Raster tetap memakai model dasar karena ruang lingkup atau bukti kalibrasi tidak cocok."}

    try:
        model_id = int(requested_id)
    except (TypeError, ValueError):
        return refused("calibration_model_id_invalid")
    model = db.get(CarbonCalibrationModel, model_id)
    if model is None or not model.is_active or model.status != "active_local":
        return refused("calibration_model_not_active")
    run = db.get(CarbonCalibrationRun, model.run_id) if model.run_id else None
    dataset = db.get(CarbonCalibrationDataset, model.dataset_pk)
    if run is None or dataset is None:
        return refused("calibration_scope_missing")
    try:
        current = manifest(model, run, dataset, db)
    except HTTPException:
        return refused("calibration_validation_invalidated")
    metadata = model.metadata_json or {}
    if metadata.get("approval_manifest") != current or model.approved_by is None:
        return refused("calibration_review_stale")
    try:
        path = Path(model.artifact_path)
        if sha256_file(path)[0] != model.artifact_sha256:
            return refused("calibration_artifact_checksum_mismatch")
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return refused("calibration_artifact_unreadable")
    if model.target_pool != target_pool or artifact.get("target_pool") != target_pool:
        return refused("calibration_target_pool_mismatch")
    if model.target_unit != unit or artifact.get("target_unit") != unit or unit != "Mg C/ha":
        return refused("calibration_unit_mismatch")
    if not _compatible_base(artifact, model_pin):
        return refused("calibration_base_model_identity_mismatch")
    sensors = _source_sensors({"data_quality": {"source_provenance": source_provenance or []}})
    allowed_sensors = {str(item).lower() for item in (artifact.get("compatible_sensors") or [])}
    if not sensors or not allowed_sensors or not sensors.issubset(allowed_sensors):
        return refused("calibration_sensor_scope_mismatch")
    try:
        domain = _geometry(artifact.get("application_domain_geojson"))
        aoi = _geometry(payload.get("aoi"))
        if domain is None or aoi is None or not domain.is_valid or not aoi.is_valid or not domain.covers(aoi):
            return refused("calibration_application_domain_mismatch")
    except Exception:
        return refused("calibration_application_domain_unreadable")
    compatible_years = artifact.get("compatible_analysis_years")
    requested_year = payload.get("year")
    if (
        not isinstance(compatible_years, list)
        or not compatible_years
        or requested_year is None
        or int(requested_year) not in {int(year) for year in compatible_years}
    ):
        return refused("calibration_analysis_year_unsupported")
    try:
        intercept, slope = float(artifact["intercept"]), float(artifact["slope"])
        clip_min = float(artifact["clip_min"])
    except (KeyError, TypeError, ValueError):
        return refused("calibration_coefficients_or_clip_rule_missing")
    if not math.isfinite(intercept) or not math.isfinite(slope) or slope < 0 or not math.isfinite(clip_min) or clip_min < 0:
        return refused("calibration_coefficients_invalid")
    validation = artifact.get("validation") if isinstance(artifact.get("validation"), dict) else {}
    info = {
        "status": "applied",
        "application_mode": "pixel_level_raster",
        "model_id": model.id,
        "model_version": model.version,
        "artifact_sha256": model.artifact_sha256,
        "validation_type": validation.get("validation_type") or validation.get("validation_method"),
        "independent_validation_metrics": validation.get("calibrated_validation") or validation.get("independent_validation_metrics"),
        "intercept": intercept,
        "slope": slope,
        "clip_min": clip_min,
        "analysis_year": int(requested_year),
        "sensor_scope": sorted(sensors),
    }
    return {**artifact, "intercept": intercept, "slope": slope, "clip_min": clip_min}, info


def apply_selected_calibration(
    db: Session,
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    model_pin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply only if every serving identity is current and the raster can be transformed.

    Current carbon result contracts return map IDs/tiles without an editable raster
    expression. Such results are intentionally marked ``not_applied`` so summary
    statistics cannot diverge from the displayed raster.
    """
    existing_model_info = result.get("model_info") if isinstance(result.get("model_info"), dict) else {}
    existing_calibration = existing_model_info.get("calibration")
    if isinstance(existing_calibration, dict) and existing_calibration.get("status") == "applied":
        return result
    requested_id = payload.get("calibration_model_id")
    if requested_id in (None, ""):
        return result
    try:
        model_id = int(requested_id)
    except (TypeError, ValueError):
        return _not_applied(result, "calibration_model_id_invalid", requested_id)
    model = db.get(CarbonCalibrationModel, model_id)
    if model is None or not model.is_active or model.status != "active_local":
        return _not_applied(result, "calibration_model_not_active", model_id)
    run = db.get(CarbonCalibrationRun, model.run_id) if model.run_id else None
    dataset = db.get(CarbonCalibrationDataset, model.dataset_pk)
    if run is None or dataset is None:
        return _not_applied(result, "calibration_scope_missing", model_id)
    try:
        current_manifest = manifest(model, run, dataset, db)
    except HTTPException:
        return _not_applied(result, "calibration_validation_invalidated", model_id)
    metadata = model.metadata_json or {}
    if metadata.get("approval_manifest") != current_manifest or model.approved_by is None:
        return _not_applied(result, "calibration_review_stale", model_id)
    artifact_path = Path(model.artifact_path)
    try:
        if sha256_file(artifact_path)[0] != model.artifact_sha256:
            return _not_applied(result, "calibration_artifact_checksum_mismatch", model_id)
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return _not_applied(result, "calibration_artifact_unreadable", model_id)

    result_model = result.get("model_info") if isinstance(result.get("model_info"), dict) else {}
    estimated = result.get("carbon_estimated") if isinstance(result.get("carbon_estimated"), dict) else {}
    reference = result.get("carbon_reference") if isinstance(result.get("carbon_reference"), dict) else {}
    actual_pool = result_model.get("target_pool") or estimated.get("target_pool") or reference.get("target_pool")
    if actual_pool != model.target_pool or artifact.get("target_pool") != model.target_pool:
        return _not_applied(result, "calibration_target_pool_mismatch", model_id)
    if not _compatible_base(artifact, model_pin):
        return _not_applied(result, "calibration_base_model_identity_mismatch", model_id)
    supported_sensors = {str(item).lower() for item in (artifact.get("compatible_sensors") or [])}
    actual_sensors = _source_sensors(result)
    if not supported_sensors or not actual_sensors or not actual_sensors.issubset(supported_sensors):
        return _not_applied(result, "calibration_sensor_scope_mismatch", model_id)
    try:
        domain = _geometry(artifact.get("application_domain_geojson"))
        aoi = _geometry(payload.get("aoi"))
        if domain is None or aoi is None or domain.is_empty or aoi.is_empty or not domain.is_valid or not aoi.is_valid or not domain.covers(aoi):
            return _not_applied(result, "calibration_application_domain_mismatch", model_id)
    except Exception:  # malformed geometry or unsupported AOI shape fails closed
        return _not_applied(result, "calibration_application_domain_unreadable", model_id)
    if model.target_unit != "Mg C/ha" or artifact.get("target_unit") != model.target_unit or estimated.get("unit") != model.target_unit:
        return _not_applied(result, "calibration_unit_mismatch", model_id)
    compatible_years = artifact.get("compatible_analysis_years")
    requested_year = payload.get("year")
    try:
        year_is_supported = (
            isinstance(compatible_years, list)
            and bool(compatible_years)
            and requested_year is not None
            and int(requested_year) in {int(year) for year in compatible_years}
        )
    except (TypeError, ValueError):
        year_is_supported = False
    if not year_is_supported:
        return _not_applied(result, "calibration_analysis_year_unsupported", model_id)
    if estimated.get("tile_url"):
        # A map URL cannot be affinely transformed here. Do not change summary
        # values while leaving the displayed raster at the uncalibrated output.
        return _not_applied(result, "pixel_level_transform_not_supported_by_analysis_output", model_id)
    try:
        intercept, slope = float(artifact["intercept"]), float(artifact["slope"])
        clip_min = float(artifact["clip_min"])
    except (KeyError, TypeError, ValueError):
        return _not_applied(result, "calibration_coefficients_or_clip_rule_missing", model_id)
    if not math.isfinite(intercept) or not math.isfinite(slope) or slope < 0 or not math.isfinite(clip_min) or clip_min < 0:
        return _not_applied(result, "calibration_coefficients_invalid", model_id)
    stats = estimated.get("statistics")
    if not isinstance(stats, dict) or any(not isinstance(stats.get(key), (int, float)) for key in ("mean", "min", "max")):
        return _not_applied(result, "carbon_statistics_missing", model_id)
    if intercept + slope * float(stats["min"]) < clip_min:
        return _not_applied(result, "pixel_level_clip_not_supported_by_summary", model_id)
    calibrated = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    calibrated_stats = calibrated["carbon_estimated"]["statistics"]
    original_stats = dict(calibrated_stats)
    for key in ("mean", "min", "max"):
        if isinstance(calibrated_stats.get(key), (int, float)):
            calibrated_stats[key] = intercept + slope * float(calibrated_stats[key])
    if isinstance(calibrated_stats.get("std_dev"), (int, float)):
        calibrated_stats["std_dev"] = abs(slope) * float(calibrated_stats["std_dev"])
    percentiles = calibrated_stats.get("percentiles")
    if isinstance(percentiles, dict):
        for key, value in list(percentiles.items()):
            if isinstance(value, (int, float)):
                percentiles[key] = intercept + slope * float(value)
    histogram = calibrated_stats.get("histogram")
    if isinstance(histogram, dict) and isinstance(histogram.get("bucket_means"), list):
        histogram["bucket_means"] = [intercept + slope * float(value) for value in histogram["bucket_means"]]
    if isinstance(calibrated_stats.get("min"), (int, float)) and calibrated_stats["min"] < clip_min:
        return _not_applied(result, "calibration_would_produce_negative_carbon", model_id)
    area = calibrated.get("area_info")
    area_ha = area.get("calculation_area_ha", area.get("area_ha")) if isinstance(area, dict) else None
    if isinstance(area, dict) and isinstance(area_ha, (int, float)):
        total_carbon = float(calibrated_stats["mean"]) * float(area_ha)
        area["total_carbon_tons"] = total_carbon
        area["carbon_dioxide_equivalent_tons"] = total_carbon * (44 / 12)
    calibrated.setdefault("model_info", {})["calibration"] = {
        "status": "applied",
        "application_mode": "statistics_only_no_map_layer",
        "model_id": model.id,
        "model_version": model.version,
        "artifact_sha256": model.artifact_sha256,
        "validation_type": (artifact.get("validation") or {}).get("validation_type"),
        "independent_validation_metrics": (artifact.get("validation") or {}).get("independent_validation_metrics"),
        "intercept": intercept,
        "slope": slope,
        "clip_min": clip_min,
        "statistics_before": original_stats,
        "warning": "No map tile was returned for this result; calibration changed the saved statistics only.",
    }
    return calibrated
