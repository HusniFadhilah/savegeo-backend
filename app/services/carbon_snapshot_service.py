"""Persist immutable carbon statistics/provenance and render safe CSV exports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models.analysis_provenance import AnalysisProvenance
from app.db.models.carbon_analysis_snapshot import CarbonAnalysisSnapshot

SNAPSHOT_SCHEMA = "savegeo-carbon-analysis-snapshot/v1"
CSV_COLUMNS = ("analysis_id", "pool", "field", "value", "unit", "status", "explanation")
_STAT_KEYS = ("mean", "min", "max", "std_dev", "valid_pixels", "pixel_count", "coverage_pct")


def _plain(value: Any) -> Any:
    """JSON-normalize known result values without persisting non-finite numbers."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _layer_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    stats = value.get("statistics")
    if isinstance(stats, dict):
        summary = {key: stats.get(key) for key in _STAT_KEYS if stats.get(key) is not None}
        histogram = stats.get("histogram")
        if isinstance(histogram, dict) and histogram.get("pixel_count") is not None:
            summary["valid_pixels" if not histogram.get("sampled") else "sampled_pixel_count"] = histogram["pixel_count"]
        result["statistics"] = _plain(summary)
    for key in ("unit", "target_pool", "quantity", "depth_cm", "source_period", "inference_mode"):
        if value.get(key) is not None:
            result[key] = _plain(value[key])
    return result


def carbon_result_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Keep stable statistics/evidence only; discard temporary map URLs and artifacts."""
    pools = result.get("pools") if isinstance(result.get("pools"), dict) else None
    if pools:
        clean_pools = {
            str(pool): carbon_result_snapshot(value)
            for pool, value in pools.items() if isinstance(value, dict)
        }
        return {"pools": clean_pools}

    model_info = result.get("model_info") if isinstance(result.get("model_info"), dict) else {}
    data_quality = result.get("data_quality") if isinstance(result.get("data_quality"), dict) else {}
    reference = result.get("carbon_reference") if isinstance(result.get("carbon_reference"), dict) else {}
    estimated = result.get("carbon_estimated") if isinstance(result.get("carbon_estimated"), dict) else {}
    allowed_model_keys = (
        "model_name", "model_version", "algorithm", "target_pool", "reference_dataset",
        "reference_dataset_year", "analysis_year", "requested_analysis_year", "reference_year",
        "scale", "summary_scale_m", "map_resolution_m", "map_resolution_crs", "reference_support_m",
        "depth_cm", "source_period", "validation_type", "validation_metrics", "validation_samples",
        "n_samples", "training_samples", "validation_note", "data_value_kind", "display_mode",
        "cv_metrics", "spatial_cv_metrics",
        "calibration",
    )
    allowed_quality_keys = (
        "valid_pixel_pct", "gap_filled", "images_used", "coefficient_of_variation_pct",
        "cloud_mask_technique", "analysis_year_partial", "source_provenance",
    )
    clean: dict[str, Any] = {
        "carbon_estimated": _layer_snapshot(estimated),
        "area_info": _plain(result.get("area_info") if isinstance(result.get("area_info"), dict) else {}),
        "model_info": _plain({key: model_info.get(key) for key in allowed_model_keys if model_info.get(key) is not None}),
        "data_quality": _plain({key: data_quality.get(key) for key in allowed_quality_keys if data_quality.get(key) is not None}),
        "source_provenance": _plain(
            data_quality.get("source_provenance") or result.get("source_provenance") or []
        ),
        "quality_assessment": _plain(result.get("quality_assessment") or {}),
    }
    clean["carbon_reference"] = {
        key: _plain(reference[key]) for key in (
            "name", "full_name", "provider", "provider_type", "year", "resolution", "unit",
            "target_pool", "quantity", "depth_cm", "source_period", "product_version", "source_url",
            "attribution",
        ) if reference.get(key) is not None
    }
    return clean


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def snapshot_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def create_carbon_snapshot(
    db: Session,
    *,
    analysis_id: str,
    owner_key: str,
    analysis_type: str,
    result: dict[str, Any],
    provenance: AnalysisProvenance | None = None,
) -> CarbonAnalysisSnapshot:
    existing = db.get(CarbonAnalysisSnapshot, analysis_id)
    if existing is not None:
        if existing.owner_key != owner_key:
            raise ValueError("Carbon snapshot owner does not match the submitting account")
        return existing
    parameters = provenance.parameters if provenance and isinstance(provenance.parameters, dict) else {}
    record = {
        "schema_version": SNAPSHOT_SCHEMA,
        "analysis_id": analysis_id,
        "analysis_type": analysis_type,
        "created_at": datetime.now(UTC).isoformat(),
        "parameters": _plain(parameters),
        "result": carbon_result_snapshot(result),
        "provenance": _plain(provenance.to_dict()) if provenance else {"id": analysis_id},
    }
    snapshot = CarbonAnalysisSnapshot(
        analysis_id=analysis_id,
        owner_key=owner_key,
        snapshot=record,
        sha256=snapshot_sha256(record),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _rows_for_result(analysis_id: str, result: dict[str, Any], parameters: dict[str, Any], snapshot_sha256: str | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pools = result.get("pools") if isinstance(result.get("pools"), dict) else None
    entries = list(pools.items()) if pools else [("", result)]
    for pool, item in entries:
        if not isinstance(item, dict):
            continue
        carbon = item.get("carbon_estimated") if isinstance(item.get("carbon_estimated"), dict) else {}
        stats = carbon.get("statistics") if isinstance(carbon.get("statistics"), dict) else {}
        area = item.get("area_info") if isinstance(item.get("area_info"), dict) else {}
        model = item.get("model_info") if isinstance(item.get("model_info"), dict) else {}
        quality = item.get("data_quality") if isinstance(item.get("data_quality"), dict) else {}
        reference = item.get("carbon_reference") if isinstance(item.get("carbon_reference"), dict) else {}
        unit = str(carbon.get("unit") or reference.get("unit") or "")
        resolved_pool = str(pool or model.get("target_pool") or carbon.get("target_pool") or "")

        def add(
            field: str,
            value: Any,
            row_unit: str = "",
            status: str = "available",
            note: str = "",
            pool_name: str = resolved_pool,
        ) -> None:
            rows.append({
                "analysis_id": analysis_id, "pool": pool_name, "field": field,
                "value": "" if value is None else _format_value(value), "unit": row_unit,
                "status": status, "explanation": note,
            })

        for key in ("aoi_name", "analysis_area_name", "year", "start_date", "end_date", "start_month", "end_month", "reference_dataset", "dataset_year", "scale", "cloud_mask_technique"):
            if parameters.get(key) is not None:
                add(f"analysis.{key}", parameters[key], "m" if key == "scale" else "")
        for key in ("name", "full_name", "provider", "provider_type", "year", "resolution", "target_pool", "quantity", "depth_cm", "source_period", "product_version"):
            if reference.get(key) is not None:
                add(f"source.{key}", reference[key], "m" if key == "resolution" else "")
        for key in ("mean", "min", "max", "std_dev", "valid_pixels", "pixel_count"):
            if stats.get(key) is not None:
                add(f"statistics.{key}", stats[key], unit)
        absent_statistics = [key for key in ("mean", "min", "max", "std_dev", "valid_pixels") if stats.get(key) is None]
        if absent_statistics:
            add("statistics.unavailable_fields", absent_statistics, "", "not_available",
                "Metrik berikut tidak tersedia pada hasil tersimpan dan tidak diganti dengan nol.")
        if area:
            for key in ("area_ha", "calculation_area_ha", "valid_area_ha", "total_carbon_tons", "carbon_dioxide_equivalent_tons"):
                if area.get(key) is not None:
                    area_unit = "ha" if key.endswith("area_ha") else "t C" if key == "total_carbon_tons" else "t CO2e" if key.endswith("equivalent_tons") else ""
                    add(f"area.{key}", area[key], area_unit)
        coverage = quality.get("valid_pixel_pct")
        if coverage is not None:
            add("quality.valid_pixel_pct", coverage, "%")
        else:
            add("quality.valid_pixel_pct", None, "%", "not_available", "Cakupan piksel valid tidak dikembalikan oleh sumber analisis ini; nilai tidak diisi dengan nol.")
        if quality.get("images_used") is not None:
            add("quality.images_used", quality["images_used"], "scene")
        validation_type = model.get("validation_type") or "not_validated"
        validation_metrics = model.get("validation_metrics") or model.get("spatial_cv_metrics") or model.get("cv_metrics")
        add("validation.type", validation_type, "", "available" if validation_type != "not_validated" else "not_available",
            "Status ini menjelaskan bukti evaluasi model, bukan validasi lapangan." if validation_type != "not_validated" else "Tidak ada bukti validasi yang tercatat untuk hasil ini.")
        for key in ("n_samples", "training_samples", "validation_samples"):
            if model.get(key) is not None:
                add(f"validation.{key}", model[key], "sample")
        if isinstance(validation_metrics, dict):
            for key in ("rmse", "mae", "bias", "r2"):
                if validation_metrics.get(key) is not None:
                    add(f"validation.{key}", validation_metrics[key], unit)
        for key in ("model_name", "model_version", "algorithm", "reference_dataset", "reference_dataset_year", "analysis_year", "reference_year", "summary_scale_m", "map_resolution_m", "map_resolution_crs"):
            if model.get(key) is not None:
                add(f"model.{key}", model[key], "m" if key.endswith("_m") else "")
        calibration = model.get("calibration")
        if calibration is not None:
            add("calibration", calibration)
        source_provenance = item.get("source_provenance") or quality.get("source_provenance")
        if source_provenance:
            add("source.provenance", source_provenance)
        else:
            add("source.provenance", None, "", "not_available", "Metadata scene/sumber rinci tidak tersedia pada hasil ini.")
        for key in ("gap_filled", "cloud_mask_technique", "analysis_year_partial"):
            if quality.get(key) is not None:
                add(f"quality.{key}", quality[key])
        if snapshot_sha256:
            add("snapshot.sha256", snapshot_sha256)
    return rows


def build_statistics_csv(snapshot: dict[str, Any]) -> bytes:
    """Create spreadsheet-friendly UTF-8 CSV from the saved snapshot document."""
    analysis_id = str(snapshot.get("analysis_id") or "")
    result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
    parameters = snapshot.get("parameters") if isinstance(snapshot.get("parameters"), dict) else {}
    rows = _rows_for_result(analysis_id, result, parameters, snapshot.get("_snapshot_sha256"))
    provenance = snapshot.get("provenance") if isinstance(snapshot.get("provenance"), dict) else {}
    for field, value in (
        ("analysis.type", snapshot.get("analysis_type")),
        ("snapshot.created_at", snapshot.get("created_at")),
        ("provenance.id", provenance.get("id") or analysis_id),
    ):
        if value is not None:
            rows.insert(0, {"analysis_id": analysis_id, "pool": "", "field": field,
                            "value": _format_value(value), "unit": "", "status": "available", "explanation": ""})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def safe_csv_filename(value: str | None, analysis_id: str) -> str:
    raw = str(value or "")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("._-")[:48]
    return f"savegeo_carbon_{slug or analysis_id[:12]}.csv"
