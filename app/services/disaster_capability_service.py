"""Capability and publication checks for persisted disaster analyses.

The registry is intentionally descriptive: an enabled method can still be an
indicator-only method and must not be presented as a trained damage model.
This module is the single validation boundary shared by admin and public
disaster routes.  It contains no Earth Engine calls, so it is safe to use in
catalog audits and unit tests.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.registries.disaster_model_registry import get_model


@dataclass(frozen=True)
class CapabilityCheck:
    allowed: bool
    status: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "reasons": list(self.reasons),
        }


def _normalise(value: object) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _source_kind(image: object) -> str:
    return str(getattr(image, "source_kind", "gee") or "gee").casefold()


def _sensor_text(image: object) -> str:
    return _normalise(
        " ".join(
            str(getattr(image, field, "") or "")
            for field in ("satellite", "sensor", "data_source")
        )
    )


def _contains_sensor(text: str, accepted: Iterable[str]) -> bool:
    return any(_normalise(alias) in text for alias in accepted)


def model_state(model: dict | None) -> str:
    """Map registry metadata to the honest user-facing capability state."""
    if not model or not model.get("enabled"):
        return "not_available"
    if not model.get("damage_model", False):
        return "indicator_only"
    if model.get("validation_status") in {"review_required", "method_validation_only"}:
        return "review_required"
    return "validated"


def check_inputs(
    model_id: str,
    event: object | None,
    aoi: object | None,
    pre_imagery: object | None,
    post_imagery: object | None,
) -> CapabilityCheck:
    """Validate registry compatibility without contacting an external source."""
    model = get_model(model_id)
    if not model:
        return CapabilityCheck(False, "not_available", (f"Model {model_id} tidak terdaftar",))
    if not model.get("enabled"):
        reason = model.get("availability_reason") or model.get("description") or "Implementasi model belum tersedia"
        return CapabilityCheck(False, "not_available", (reason,))
    if event is None:
        return CapabilityCheck(False, "not_available", ("Event tidak tersedia",))

    reasons: list[str] = []
    disaster_type = getattr(event, "disaster_type", None)
    if disaster_type not in model.get("disaster_types", []):
        reasons.append(f"Model tidak mendukung jenis bencana {disaster_type}")
    if aoi is None or not getattr(aoi, "geojson", None):
        reasons.append("AOI belum tersedia atau kosong")
    elif getattr(event, "id", None) is not None and getattr(aoi, "event_id", None) not in (None, event.id):
        reasons.append("AOI berasal dari event lain")

    required_phases = []
    if model.get("requires_pre", False):
        required_phases.append(("pre", pre_imagery))
    if model.get("requires_post", False):
        required_phases.append(("post", post_imagery))

    for phase, image in required_phases:
        if image is None:
            reasons.append(f"Imagery {phase} belum tersedia")
            continue
        if getattr(image, "phase", None) != phase:
            reasons.append(f"Imagery {phase} berasal dari fase yang salah")
        if getattr(event, "id", None) is not None and getattr(image, "event_id", None) not in (None, event.id):
            reasons.append(f"Imagery {phase} berasal dari event lain")
        source_kind = _source_kind(image)
        if source_kind not in {str(v).casefold() for v in model.get("accepted_source_kind", [])}:
            reasons.append(
                f"Source kind {source_kind} tidak kompatibel; diterima: "
                f"{', '.join(model.get('accepted_source_kind', []))}"
            )
        sensor_text = _sensor_text(image)
        if model.get("accepted_sensors") and not _contains_sensor(sensor_text, model["accepted_sensors"]):
            reasons.append(
                f"Sensor imagery {phase} tidak kompatibel; diterima: "
                f"{', '.join(model.get('accepted_sensors', []))}"
            )
        if not getattr(image, "acquisition_date", None):
            reasons.append(f"Tanggal imagery {phase} tidak tersedia")
        resolution = model.get("resolution_m")
        image_resolution = getattr(image, "resolution_m", None)
        if resolution is not None and image_resolution is None:
            reasons.append(f"Metadata resolusi imagery {phase} belum tersedia; model memerlukan {resolution} m")
        elif resolution is not None and float(image_resolution) != float(resolution):
            reasons.append(f"Resolusi imagery {phase} harus {resolution} m")

    if pre_imagery is not None and post_imagery is not None:
        pre_date = getattr(pre_imagery, "acquisition_date", None)
        post_date = getattr(post_imagery, "acquisition_date", None)
        if pre_date and post_date and pre_date >= post_date:
            reasons.append("Tanggal pre harus lebih awal dari post")

    if reasons:
        incompatible = any(
            phrase in reason
            for reason in reasons
                for phrase in ("mendukung", "kompatibel", "Source kind", "Sensor imagery", "Resolusi")
        )
        return CapabilityCheck(False, "incompatible_input" if incompatible else "not_available", tuple(reasons))
    return CapabilityCheck(True, model_state(model), ())


def build_input_fingerprint(
    event: object,
    aoi: object,
    pre_imagery: object | None,
    post_imagery: object | None,
    model: dict,
    parameters: dict | None = None,
) -> str:
    """Stable cache/provenance key for the actual inputs used by a run."""
    payload = {
        "event_id": getattr(event, "id", None),
        "aoi_id": getattr(aoi, "id", None),
        "aoi_geojson": getattr(aoi, "geojson", None),
        "model_id": model.get("model_id"),
        "model_version": model.get("version"),
        "parameters": parameters or {},
        "imagery": [
            {
                "id": getattr(image, "id", None),
                "phase": getattr(image, "phase", None),
                "source_kind": getattr(image, "source_kind", None),
                "satellite": getattr(image, "satellite", None),
                "sensor": getattr(image, "sensor", None),
                "date": getattr(getattr(image, "acquisition_date", None), "isoformat", lambda: None)(),
                "resolution_m": getattr(image, "resolution_m", None),
            }
            for image in (pre_imagery, post_imagery)
            if image is not None
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_provenance(
    event: object,
    aoi: object,
    pre_imagery: object | None,
    post_imagery: object | None,
    model: dict,
    *,
    parameters: dict | None = None,
    validation_status: str | None = None,
) -> dict[str, Any]:
    images = [image for image in (pre_imagery, post_imagery) if image is not None]
    return {
        "event_id": getattr(event, "id", None),
        "aoi_id": getattr(aoi, "id", None),
        "aoi_fingerprint": build_input_fingerprint(event, aoi, None, None, model, parameters),
        "imagery": [
            {
                "id": getattr(image, "id", None),
                "phase": getattr(image, "phase", None),
                "sensor": getattr(image, "sensor", None) or getattr(image, "satellite", None),
                "satellite": getattr(image, "satellite", None),
                "source_kind": getattr(image, "source_kind", None),
                "acquisition_date": (
                    getattr(getattr(image, "acquisition_date", None), "isoformat", lambda: None)()
                ),
                "resolution_m": getattr(image, "resolution_m", None),
                "cloud_coverage_pct": getattr(image, "cloud_coverage_pct", None),
                "scene_id": getattr(image, "scene_id", None),
            }
            for image in images
        ],
        "source_ids": [getattr(image, "id", None) for image in images],
        "source_kind": sorted({_source_kind(image) for image in images}),
        "sensor": sorted({_sensor_text(image) for image in images}),
        "model_id": model.get("model_id"),
        "model_version": model.get("version"),
        "parameters": parameters or {},
        "input_fingerprint": build_input_fingerprint(event, aoi, pre_imagery, post_imagery, model, parameters),
        "validation_status": validation_status or model_state(model),
        "limitations": list(model.get("limitations", [])),
    }


def validate_persisted_result(
    db, event: object, run: object, result: object, *, require_published: bool = True
) -> CapabilityCheck:
    """Re-check a result at read/publish time; registry changes invalidate old output."""
    model = get_model(getattr(run, "model_id", ""))
    if not model or not model.get("enabled"):
        return CapabilityCheck(False, "not_available", ("Model hasil sudah tidak tersedia",))
    if getattr(run, "status", None) == "stale":
        return CapabilityCheck(False, "stale", ("Analysis run ditandai stale dan harus dihitung ulang",))
    if require_published and not getattr(result, "is_published", False):
        return CapabilityCheck(False, "not_available", ("Hasil belum dipublikasikan",))
    if not getattr(result, "statistics", None) or not getattr(result, "tile_url", None):
        return CapabilityCheck(False, "not_available", ("Tile atau statistik hasil belum lengkap",))
    provenance = getattr(result, "provenance", None) or {}
    required = {"event_id", "aoi_id", "imagery", "model_id", "model_version", "input_fingerprint"}
    missing = sorted(required.difference(provenance))
    if missing:
        return CapabilityCheck(False, "stale", (f"Provenance tidak lengkap: {', '.join(missing)}",))
    if provenance.get("event_id") != getattr(event, "id", None):
        return CapabilityCheck(False, "stale", ("Provenance event tidak cocok",))
    if provenance.get("model_id") != model.get("model_id") or provenance.get("model_version") != model.get("version"):
        return CapabilityCheck(False, "stale", ("Versi model hasil berbeda dari registry",))

    from app.repositories import disaster_repo

    aoi = disaster_repo.get_aoi(db, run.aoi_id)
    pre = disaster_repo.get_imagery(db, run.pre_imagery_id) if run.pre_imagery_id else None
    post = disaster_repo.get_imagery(db, run.post_imagery_id) if run.post_imagery_id else None
    inputs = check_inputs(model["model_id"], event, aoi, pre, post)
    if not inputs.allowed:
        return inputs
    current_fingerprint = build_input_fingerprint(event, aoi, pre, post, model)
    if provenance.get("input_fingerprint") != current_fingerprint:
        return CapabilityCheck(False, "stale", ("Input imagery atau AOI berubah; hasil harus dihitung ulang",))
    return CapabilityCheck(True, inputs.status, ())


def event_publication_check(db, event: object) -> CapabilityCheck:
    """Check the minimum event contract before an admin marks it published."""
    from app.repositories import disaster_repo

    reasons: list[str] = []
    if not getattr(event, "name", None) or not getattr(event, "disaster_type", None):
        reasons.append("Metadata kejadian belum lengkap")
    if not getattr(event, "source", None) and not getattr(event, "source_ids", None):
        reasons.append("Sumber resmi kejadian belum tersedia")
    aoi = disaster_repo.get_active_aoi(db, event.id)
    if aoi is None or not aoi.geojson:
        reasons.append("AOI belum tersedia")
    published_or_completed = False
    for run in disaster_repo.list_runs_for_event(db, event.id):
        if run.status not in {"completed", "review_required", "published"}:
            continue
        result = disaster_repo.get_result_for_run(db, run.id)
        if result and result.statistics and result.tile_url:
            result_check = validate_persisted_result(db, event, run, result, require_published=False)
            if not result_check.allowed:
                reasons.extend(result_check.reasons)
                continue
            published_or_completed = True
            break
    if not published_or_completed:
        reasons.append("Belum ada hasil analisis selesai dengan statistik dan tile valid")
    if reasons:
        return CapabilityCheck(False, "not_available", tuple(reasons))
    return CapabilityCheck(True, "validated", ())


def readiness_matrix(db, event: object) -> dict[str, dict[str, Any]]:
    from app.repositories import disaster_repo

    aoi = disaster_repo.get_active_aoi(db, event.id)
    pre = disaster_repo.get_primary_imagery(db, event.id, "pre")
    post = disaster_repo.get_primary_imagery(db, event.id, "post")
    runs = disaster_repo.list_runs_for_event(db, event.id)
    compatible = []
    reasons: list[str] = []
    for model in (
        get_model(run.model_id) for run in runs
    ):
        if model:
            check = check_inputs(model["model_id"], event, aoi, pre, post)
            compatible.append(check.allowed)
            reasons.extend(check.reasons)
    metadata_ready = bool(getattr(event, "name", None) and getattr(event, "disaster_type", None))
    source_ready = bool(getattr(event, "source", None) or getattr(event, "source_ids", None))
    result_ready = any(
        disaster_repo.get_result_for_run(db, run.id)
        and disaster_repo.get_result_for_run(db, run.id).statistics
        for run in runs
        if run.status in {"completed", "review_required", "published"}
    )
    return {
        "metadata_event": {"status": "ready" if metadata_ready else "missing", "reasons": [] if metadata_ready else ["Nama dan jenis bencana wajib diisi"]},
        "official_source": {"status": "ready" if source_ready else "missing", "reasons": [] if source_ready else ["Sumber resmi belum diisi"]},
        "aoi": {"status": "ready" if aoi else "missing", "reasons": [] if aoi else ["AOI belum dibuat"]},
        "imagery_pre": {"status": "ready" if pre else "missing", "reasons": [] if pre else ["Imagery pre belum tersedia"]},
        "imagery_post": {"status": "ready" if post else "missing", "reasons": [] if post else ["Imagery post belum tersedia"]},
        "compatible_model": {"status": "ready" if any(compatible) else "unavailable", "reasons": reasons},
        "qc": {"status": "passed" if result_ready else "review_required", "reasons": [] if result_ready else ["Belum ada hasil dengan statistik valid"]},
        "publication": {"status": "allowed" if event_publication_check(db, event).allowed else "blocked", "reasons": list(event_publication_check(db, event).reasons)},
    }
