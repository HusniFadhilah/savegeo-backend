"""Membership-scoped integrity records, independent review and portfolio views."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_advanced import DmrvFieldCapture
from app.db.models.dmrv_field import SurveyPlot
from app.db.models.dmrv_integrity import (
    DmrvIntegrityDocument,
    DmrvIntegrityRecord,
    DmrvIntegrityRevision,
    DmrvRiskPolicy,
)
from app.db.models.dmrv_advanced import DmrvCreditEvent
from app.db.models.dmrv_job import DmrvSnapshot
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.dmrv_submission import DmrvReviewFinding, DmrvSubmission
from app.db.models.organization import OrganizationMember
from app.schemas.dmrv_integrity import (
    AccountingInput,
    EvidenceInput,
    GrievanceInput,
    MethodologyCheckInput,
    ProjectOperationsInput,
    RecordOut,
    RecordReview,
    RecordSave,
    RegistryInput,
    RiskInput,
    SatelliteProvenanceInput,
    ValidationInput,
)
from app.schemas.dmrv_carbon_features import (
    BlueCarbonMonitoringInput,
    CommunityBenefitInput,
    VMD0054LeakageInput,
    MethodologyCalculationInput,
    RegistryReconciliationInput,
    SafeguardIndicatorInput,
    SamplingPlanInput,
    UncertaintyBudgetInput,
)
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.integrity_calculations import spatial_validation, stock_change
from app.services.dmrv import carbon_features


def _criterion(category, key, title, title_en, required=True):
    return {"category": category, "key": key, "title": title, "title_en": title_en, "required": required}


CATALOG = [
    _criterion("readiness", "legal_entity", "Legalitas pengembang", "Developer legal identity"),
    _criterion(
        "readiness", "land_tenure", "Hak pengelolaan dan batas lahan", "Land tenure and management rights"
    ),
    _criterion(
        "readiness", "methodology", "Kelayakan metodologi dan versi", "Methodology eligibility and version"
    ),
    _criterion("readiness", "baseline", "Skenario baseline dan buktinya", "Baseline scenario and evidence"),
    _criterion("readiness", "additionality", "Penilaian additionality", "Additionality assessment"),
    _criterion(
        "readiness", "monitoring_plan", "Rencana dan frekuensi monitoring", "Monitoring plan and frequency"
    ),
    _criterion("readiness", "pool_selection", "Pemilihan lima pool karbon", "Five carbon pool selection"),
    _criterion("readiness", "field_protocol", "Protokol lapangan dan QA/QC", "Field protocol and QA/QC"),
    _criterion(
        "readiness", "leakage_permanence", "Leakage dan risiko pembalikan", "Leakage and reversal risk"
    ),
    _criterion(
        "readiness", "data_licences", "Lisensi data dan ketertelusuran", "Data licences and provenance"
    ),
    _criterion("safeguard", "consent", "Persetujuan masyarakat (FPIC)", "Community consent (FPIC)"),
    _criterion("safeguard", "benefit_sharing", "Pembagian manfaat", "Benefit sharing"),
    _criterion("safeguard", "grievance", "Mekanisme pengaduan", "Grievance mechanism"),
    _criterion("safeguard", "biodiversity", "Keanekaragaman hayati", "Biodiversity safeguards"),
    _criterion(
        "safeguard",
        "social_impact",
        "Dampak sosial dan kelompok rentan",
        "Social impacts and vulnerable groups",
    ),
    _criterion(
        "safeguard",
        "cultural_sites",
        "Situs budaya dan akses tradisional",
        "Cultural sites and customary access",
        False,
    ),
]
SCHEMAS = {
    "operations": ProjectOperationsInput,
    "readiness": EvidenceInput,
    "safeguard": EvidenceInput,
    "registry": RegistryInput,
    "accounting": AccountingInput,
    "validation": ValidationInput,
    "methodology_check": MethodologyCheckInput,
    "satellite_provenance": SatelliteProvenanceInput,
    "risk": RiskInput,
    "grievance": GrievanceInput,
    "methodology_calc": MethodologyCalculationInput,
    "uncertainty": UncertaintyBudgetInput,
    "blue_carbon": BlueCarbonMonitoringInput,
    "vmd0054_leakage": VMD0054LeakageInput,
    "sampling_plan": SamplingPlanInput,
    "registry_import": RegistryReconciliationInput,
    "safeguard_metric": SafeguardIndicatorInput,
    "community_benefit": CommunityBenefitInput,
}
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def document_root() -> Path:
    import os

    return Path(
        os.environ.get(
            "DMRV_INTEGRITY_DIR", str(Path(__file__).resolve().parents[3] / "var" / "dmrv" / "integrity")
        )
    ).resolve()


def document_path(row: DmrvIntegrityDocument) -> Path:
    # The filesystem name is generated by the server, never supplied by the caller.
    return document_root() / row.project_id / row.id


def _documents_exist(db: Session, project_id: str, evidence: list[str]) -> None:
    for reference in evidence:
        if reference.startswith("document:"):
            ident = reference[9:]
            row = db.get(DmrvIntegrityDocument, ident)
            if row is None or row.project_id != project_id:
                raise DmrvError(422, "invalid_evidence", "Dokumen bukti harus berasal dari proyek ini.")
        else:
            parsed = urlparse(reference)
            if (
                len(reference) > 2000
                or parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise DmrvError(
                    422, "invalid_evidence", "Gunakan document:ID atau tautan HTTPS tanpa kredensial."
                )


def _evidence_references(validated) -> list[str]:
    references = list(getattr(validated, "evidence", []))
    for item in getattr(validated, "items", []):
        references.extend(item.evidence)
    return references


def _field_capture_evidence(db: Session, project_id: str, capture_ids: list[str], period: str) -> dict:
    if not capture_ids:
        return {"capture_count": 0, "captures": [], "qa_ready": False}
    if len(set(capture_ids)) != len(capture_ids):
        raise DmrvError(422, "duplicate_field_capture", "Catatan lapangan tidak boleh dipilih berulang.")
    captures = db.execute(select(DmrvFieldCapture).where(
        DmrvFieldCapture.project_id == project_id,
        DmrvFieldCapture.capture_id.in_(capture_ids),
    )).scalars().all()
    if len(captures) != len(capture_ids):
        raise DmrvError(422, "field_capture_not_found", "Semua bukti lapangan harus berasal dari proyek ini.")
    target_year = str(period)[:4]
    summaries = [{
        "capture_id": row.capture_id,
        "plot_code": row.plot_code,
        "observed_at": row.observed_at.isoformat(),
        "photo_attached": bool(row.photo_document_id),
        "qa_flags": list(row.qa_flags or []),
    } for row in sorted(captures, key=lambda item: item.observed_at)]
    wrong_period = [row for row in summaries if row["observed_at"][:4] != target_year]
    if wrong_period:
        raise DmrvError(422, "field_capture_period_mismatch", "Tanggal catatan lapangan harus berada di tahun periode indikator/manfaat.")
    qa_ready = all(not row["qa_flags"] for row in summaries)
    return {"capture_count": len(summaries), "captures": summaries, "qa_ready": qa_ready}


def out(row: DmrvIntegrityRecord) -> dict:
    return RecordOut.model_validate(row).model_dump(mode="json")


def retain_revision(
    db: Session,
    row: DmrvIntegrityRecord,
    principal: Principal,
    before: dict | None = None,
    reason: str | None = None,
    request_id: str | None = None,
) -> None:
    snapshot = out(row)
    digest = hashlib.sha256(canonical(snapshot)).hexdigest()
    db.add(
        DmrvIntegrityRevision(
            id=str(uuid.uuid4()), record_id=row.id, revision=row.revision, snapshot=snapshot, sha256=digest
        )
    )
    record_audit(
        db,
        principal=principal,
        action="integrity.record.change",
        resource_type="integrity_record",
        resource_id=row.id,
        project_id=row.project_id,
        before=before,
        after=snapshot,
        reason=reason,
        request_id=request_id,
        snapshot_hash=digest,
    )


def _record(db: Session, project_id: str, record_id: str) -> DmrvIntegrityRecord:
    row = db.get(DmrvIntegrityRecord, record_id)
    if row is None or row.project_id != project_id:
        raise DmrvError(404, "not_found", "Penilaian tidak ditemukan.")
    return row


def _calculated(db: Session, project_id: str, category: str, data) -> dict:
    if category == "operations":
        return {
            "qualification": "internal_project_record",
            "externally_verified": False,
        }
    if category == "grievance":
        days_open = max(0, (dt.datetime.now(dt.UTC).date() - data.received_on).days)
        return {
            "received_on": data.received_on.isoformat(),
            "days_open_at_last_update": days_open,
            "qualification": "Internal response-time tracking. This is not a legal or registry deadline.",
        }
    if category == "methodology_calc":
        from app.services.dmrv.methodology import normalize
        project = db.get(DmrvProject, project_id)
        if project is None or normalize(project.methodology_id) not in {"vm0047", "verravm0047"} or normalize(project.methodology_version) not in {"1.1", "v1.1"}:
            raise DmrvError(422, "methodology_mismatch", "Proyek harus memakai VM0047 v1.1 yang sama dengan input kalkulasi.")
        period = None
        if data.monitoring_period_id:
            period = db.execute(select(DmrvMonitoringPeriod).where(
                DmrvMonitoringPeriod.id == data.monitoring_period_id,
                DmrvMonitoringPeriod.project_id == project_id,
            )).scalar_one_or_none()
            if period is None:
                raise DmrvError(422, "invalid_period", "Pilih periode monitoring yang terdaftar pada proyek ini.")
            if data.period_start < period.start_date or data.period_end > period.end_date:
                raise DmrvError(422, "period_outside_monitoring_period", "Tanggal kalkulasi harus berada di dalam periode monitoring yang dipilih.")
            boundary = db.get(BoundaryVersion, period.boundary_version_id)
        else:
            boundary = db.execute(select(BoundaryVersion).where(
                BoundaryVersion.project_id == project_id,
            ).order_by(BoundaryVersion.version.desc()).limit(1)).scalar_one_or_none()
        if boundary is None:
            raise DmrvError(422, "project_boundary_required", "Tambahkan batas proyek tervalidasi sebelum menjalankan kalkulator karbon.")
        area_tolerance = max(0.01, boundary.area_ha * 0.001)
        if abs(data.area_ha - boundary.area_ha) > area_tolerance:
            raise DmrvError(422, "project_area_mismatch", "Luas input berbeda dari batas proyek tersimpan. Muat ulang proyek dan gunakan luas dari versi batas yang dipilih.")
        calculation_input = data.model_copy(update={"area_ha": boundary.area_ha})
        leakage_record = None
        if data.leakage_record_id:
            if data.approach != "area_based":
                raise DmrvError(422, "census_leakage_not_applicable", "Catatan VMD0054 tidak ditautkan pada kalkulasi census-based VM0047.")
            leakage_record = _record(db, project_id, data.leakage_record_id)
            if leakage_record.category != "vmd0054_leakage" or leakage_record.status != "accepted":
                raise DmrvError(422, "reviewed_vmd0054_required", "Pilih hasil VMD0054 yang sudah diterima reviewer independen.")
            leakage_total = (leakage_record.result or {}).get("total_leakage_tco2e")
            if not isinstance(leakage_total, (int, float)) or leakage_total < 0:
                raise DmrvError(422, "vmd0054_result_unavailable", "Hasil leakage pada catatan VMD0054 tidak valid.")
            calculation_input = calculation_input.model_copy(update={"leakage_tco2e": float(leakage_total)})
        result = carbon_features.methodology_calculation(calculation_input)
        result["leakage_calculation_record_id"] = leakage_record.id if leakage_record else None
        if data.uncertainty_record_id:
            uncertainty_record = _record(db, project_id, data.uncertainty_record_id)
            if uncertainty_record.category != "uncertainty" or uncertainty_record.status != "accepted":
                raise DmrvError(422, "reviewed_uncertainty_required", "Pilih anggaran ketidakpastian yang sudah diterima reviewer independen.")
            budget = uncertainty_record.result or {}
            interval = budget.get("interval_tco2e")
            central = budget.get("central_net_tco2e")
            net = result.get("net_screening_tco2e")
            if (not isinstance(interval, list) or len(interval) != 2
                    or not all(isinstance(value, (int, float)) for value in interval)
                    or not isinstance(central, (int, float)) or not isinstance(net, (int, float))
                    or abs(float(central) - float(net)) > max(1e-6, abs(float(net)) * 1e-8)):
                raise DmrvError(422, "uncertainty_central_mismatch", "Nilai pusat anggaran harus sama dengan hasil neto kalkulator VM0047 untuk periode ini.")
            result["linked_uncertainty_assessment"] = {
                "record_id": uncertainty_record.id,
                "confidence_level_pct": budget.get("confidence_level_pct"),
                "interval_tco2e": interval,
                "largest_uncertainty_source": budget.get("largest_uncertainty_source"),
                "combined_standard_uncertainty_tco2e": budget.get("combined_standard_uncertainty_tco2e"),
                "deducted_from_vm0047_net": False,
                "note": "Supplemental uncertainty range from a separately reviewed record; it is not applied as the methodology-specific uncertainty deduction.",
            }
        if period is None:
            result["warnings"].append("monitoring_period_not_linked_to_methodology_calculation")
            result["calculation_status"] = "review_required"
        result["project_context"] = {
            "monitoring_period_id": period.id if period else None,
            "monitoring_period_name": period.name if period else None,
            "boundary_version": boundary.version,
            "boundary_checksum": boundary.checksum,
            "area_ha_used": boundary.area_ha,
        }
        return result
    if category == "uncertainty":
        return carbon_features.uncertainty_budget(data)
    if category == "blue_carbon":
        return carbon_features.blue_carbon_monitoring(data)
    if category == "vmd0054_leakage":
        return carbon_features.vmd0054_leakage(data)
    if category == "sampling_plan":
        try:
            period = None
            if data.monitoring_period_id:
                period = db.execute(select(DmrvMonitoringPeriod).where(
                    DmrvMonitoringPeriod.id == data.monitoring_period_id,
                    DmrvMonitoringPeriod.project_id == project_id,
                )).scalar_one_or_none()
                if period is None:
                    raise DmrvError(422, "invalid_period", "Pilih periode monitoring yang terdaftar pada proyek ini.")
                boundary = db.get(BoundaryVersion, period.boundary_version_id)
            else:
                boundary = db.execute(select(BoundaryVersion).where(
                    BoundaryVersion.project_id == project_id,
                ).order_by(BoundaryVersion.version.desc()).limit(1)).scalar_one_or_none()
            if boundary is None:
                raise DmrvError(422, "project_boundary_required", "Batas proyek tervalidasi diperlukan untuk memeriksa dan menurunkan koordinat sampel.")
            result = carbon_features.sampling_plan(data)
            from shapely.geometry import Point, shape
            project_shape = shape(boundary.validated_geojson)
            for stratum in data.strata:
                if stratum.geometry is not None:
                    stratum_shape = shape(stratum.geometry)
                    if not stratum_shape.is_valid or not project_shape.covers(stratum_shape):
                        raise DmrvError(422, "stratum_outside_project_boundary", f"Geometri strata '{stratum.name}' harus valid dan sepenuhnya berada dalam batas proyek.")
            points = [point for stratum in result["strata"] for point in (stratum["plot_locations"] or [])]
            if any(not project_shape.covers(Point(point["lon"], point["lat"])) for point in points):
                raise DmrvError(422, "sample_outside_project_boundary", "Koordinat sampel harus berada di dalam batas proyek.")
            strata_area = sum(item.area_ha for item in data.strata)
            result["project_context"] = {
                "monitoring_period_id": period.id if period else None,
                "monitoring_period_name": period.name if period else None,
                "boundary_version": boundary.version,
                "boundary_checksum": boundary.checksum,
                "project_area_ha": boundary.area_ha,
                "strata_area_ha": strata_area,
                "strata_area_coverage_pct": round(strata_area / boundary.area_ha * 100, 2) if boundary.area_ha else None,
            }
            if boundary.area_ha and abs(strata_area - boundary.area_ha) / boundary.area_ha > 0.05:
                result["warnings"].append("sampling_strata_area_differs_from_project_area_by_more_than_5pct")
            return result
        except (ValueError, ImportError) as exc:
            raise DmrvError(422, "invalid_sampling_geometry", str(exc)) from None
    if category == "registry_import":
        events = db.execute(select(DmrvCreditEvent).where(DmrvCreditEvent.project_id == project_id)).scalars().all()
        internal = [{
            "event_id": item.event_id,
            "registry_name": item.registry_name,
            "batch_reference": item.batch_reference,
            "external_transaction_id": item.external_transaction_id,
            "quantity": item.quantity,
            "effective_date": item.effective_date.isoformat(),
            "event_type": item.event_type,
            "owner_from": item.owner_from,
            "owner_to": item.owner_to,
        } for item in events]
        registry_record = db.execute(
            select(DmrvIntegrityRecord).where(
                DmrvIntegrityRecord.project_id == project_id,
                DmrvIntegrityRecord.category == "registry",
            ).order_by(DmrvIntegrityRecord.updated_at.desc()).limit(1)
        ).scalar_one_or_none()
        internal_project_id = None
        if registry_record and str(registry_record.payload.get("registry", "")).casefold() == data.registry.casefold():
            internal_project_id = registry_record.payload.get("external_project_id")
        return carbon_features.registry_reconciliation(data, internal, internal_project_id)
    if category == "safeguard_metric":
        result = carbon_features.safeguard_indicator(data)
        result["field_validation"] = _field_capture_evidence(db, project_id, data.field_capture_ids, data.period)
        return result
    if category == "community_benefit":
        result = carbon_features.community_benefit(data)
        result["field_validation"] = _field_capture_evidence(db, project_id, data.field_capture_ids, data.period)
        return result
    if category == "methodology_check":
        from app.services.dmrv.methodology import validate_checklist

        validate_checklist(db, project_id, data.approach, [item.model_dump(mode="json") for item in data.items])
        return {
            "complete": sum(item.status == "complete" for item in data.items),
            "partial": sum(item.status == "partial" for item in data.items),
            "not_assessed": sum(item.status == "not_assessed" for item in data.items),
            "approach": data.approach,
            "qualification": "Internal evidence checklist only; not methodology eligibility or certification.",
        }
    if category == "satellite_provenance":
        snapshot = db.execute(
            select(DmrvSnapshot).where(
                DmrvSnapshot.id == data.snapshot_id, DmrvSnapshot.project_id == project_id
            )
        ).scalar_one_or_none()
        if snapshot is None:
            raise DmrvError(422, "snapshot_not_found", "Pilih snapshot analisis dari proyek ini.")
        source = {**(snapshot.inputs or {}), **(snapshot.outputs or {})}
        wanted = {
            "data_source": {"source", "provider", "dataset", "dataset_id", "gee_collection", "product", "asset", "collection"},
            "product_version": {"product_version", "version", "processing_version"},
            "processing_algorithm": {"algorithm"},
            "savegeo_processing_version": {"adapter_version"},
            "model_used": {"model_name"},
            "model_version": {"model_version"},
            "reference_dataset": {"reference_dataset"},
            "spatial_resolution_m": {"resolution_m", "spatial_resolution_m", "pixel_size_m", "native_scale_m"},
            "analysis_scale_m": {"scale"},
            "observation_count": {"observation_count", "scene_count", "image_count", "n_observations", "images_used", "collection_size"},
            "cloud_cover_pct": {"cloud_cover_pct", "cloud_pct", "cloud_fraction_pct", "mean_cloud_pct"},
            "valid_aoi_pixel_pct": {"valid_pixel_pct"},
            "configured_cloud_threshold_pct": {"cloud_threshold"},
            "cloud_mask_technique": {"cloud_mask_technique"},
            "gap_filled": {"gap_filled"},
            "analysis_year_partial": {"analysis_year_partial"},
            "inference_date_range": {"inference_date_range"},
            "start_month": {"start_month"},
            "end_month": {"end_month"},
            "aerosol_method_or_qa": {"aerosol_qa", "aerosol_method", "aerosol_correction", "atmospheric_correction", "aerosol_handling", "aot_band_available_in_source_product"},
            "observation_start": {"observation_start", "start_date", "date_start", "start"},
            "observation_end": {"observation_end", "end_date", "date_end"},
            "season_window": {"season", "season_window", "seasonal_window"},
            "temporal_cadence": {"temporal_cadence", "cadence", "period"},
            "provider_scene_manifest": {"source_provenance", "provider_scene_manifest", "scene_manifest"},
        }
        found: dict[str, object] = {}
        queue = [source]
        visited = 0
        while queue and visited < 3000:
            node = queue.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    visited += 1
                    normalized = str(key).lower().replace("-", "_").replace(" ", "_")
                    if isinstance(value, dict):
                        queue.append(value)
                    elif isinstance(value, list) and value and isinstance(value[0], dict):
                        if normalized in wanted["provider_scene_manifest"] and "provider_scene_manifest" not in found:
                            found["provider_scene_manifest"] = value[:100]
                        queue.extend(value[:100])
                    elif isinstance(value, (str, int, float, bool)):
                        for field, aliases in wanted.items():
                            if normalized in aliases and field not in found:
                                found[field] = value
            elif isinstance(node, list):
                queue.extend(item for item in node[:100] if isinstance(item, dict))
        if found.get("start_month") is not None and found.get("end_month") is not None:
            found["season_window"] = f"months {found['start_month']}-{found['end_month']}"
        warnings = []
        if data.aerosol_status in {"unknown", "not_corrected"}:
            warnings.append("aerosol_correction_not_confirmed")
        if data.cloud_cover_pct is None:
            warnings.append("cloud_cover_not_reported")
        if data.spatial_resolution_m is None:
            warnings.append("spatial_resolution_not_reported")
        if data.observation_count == 0:
            warnings.append("no_observations_reported")
        if data.cloud_cover_pct is not None and data.cloud_cover_pct > 30:
            warnings.append("high_cloud_cover_review_recommended")
        if data.observation_count is not None and data.observation_count < 3:
            warnings.append("few_observations_review_recommended")
        valid_pixel_pct = found.get("valid_aoi_pixel_pct")
        if isinstance(valid_pixel_pct, (int, float)) and valid_pixel_pct < 70:
            warnings.append("low_valid_aoi_pixel_coverage_review_recommended")
        if found.get("gap_filled") is True:
            warnings.append("temporal_gap_filling_in_analysis_review_recommended")
        if found.get("analysis_year_partial") is True:
            warnings.append("current_year_partial_observation_window")
        if found.get("aerosol_method_or_qa") and data.aerosol_status == "unknown":
            warnings.append("snapshot_contains_atmospheric_qa_review_metadata")
        if not found.get("provider_scene_manifest"):
            warnings.append("scene_level_acquisition_ids_not_retained_by_this_snapshot")
        return {
            "snapshot_hash": snapshot.snapshot_hash,
            "snapshot_kind": snapshot.kind,
            "automatically_extracted_metadata": found,
            "automated_quality_warnings": warnings,
            "warnings": warnings,
            "source_attestation": "provider_metadata_captured_during_original_analysis_and_bound_to_snapshot" if found.get("provider_scene_manifest") else "snapshot_metadata_extracted_not_provider_verified",
            "qualification": "Metadata shown here was extracted from the immutable SaveGeo snapshot inputs/outputs where available; absent provider properties remain unknown, and extracted values are not an independent provider verification.",
        }
    if category == "validation":
        from shapely.geometry import Point, shape
        from shapely.prepared import prep

        result = spatial_validation(data)
        boundary = db.execute(
            select(BoundaryVersion)
            .where(BoundaryVersion.project_id == project_id)
            .order_by(BoundaryVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if boundary:
            region = prep(shape(boundary.validated_geojson))
            inside = sum(region.covers(Point(p.lon, p.lat)) for p in data.pairs)
            result.update(
                inside_boundary_n=inside,
                outside_boundary_n=len(data.pairs) - inside,
                boundary_version=boundary.version,
                boundary_checksum=boundary.checksum,
            )
            if inside != len(data.pairs):
                result["warnings"].append("validation_points_outside_project")
        else:
            result["warnings"].append("project_boundary_not_available")
        return result
    if category != "accounting":
        return {}
    project = db.get(DmrvProject, project_id)
    if (project.methodology_id, project.methodology_version) != (
        data.methodology_id,
        data.methodology_version,
    ):
        raise DmrvError(
            422,
            "methodology_mismatch",
            "Isi metodologi dan versi proyek agar cocok dengan profil perhitungan.",
        )
    period = db.get(DmrvMonitoringPeriod, data.period_id)
    if period is None or period.project_id != project_id:
        raise DmrvError(422, "invalid_period", "Pilih periode monitoring proyek ini.")
    boundary = db.get(BoundaryVersion, period.boundary_version_id)
    result = stock_change(data, boundary.area_ha)
    result.update(
        boundary_version=boundary.version,
        boundary_checksum=boundary.checksum,
        period_start=period.start_date.isoformat(),
        period_end=period.end_date.isoformat(),
    )
    return result


def save_record(
    db: Session,
    principal: Principal,
    project_id: str,
    category: str,
    key: str,
    data: RecordSave,
    expected: int,
    request_id: str | None = None,
) -> dict:
    authorize(db, project_id, principal, "monitoring.write")
    if category not in SCHEMAS or not KEY_RE.fullmatch(key):
        raise DmrvError(422, "invalid_category", "Kategori atau kunci penilaian tidak valid.")
    if category in {"readiness", "safeguard"} and not any(
        c["category"] == category and c["key"] == key for c in CATALOG
    ):
        raise DmrvError(422, "unknown_criterion", "Gunakan butir dari katalog penilaian.")
    if category == "methodology_check" and key != "current":
        raise DmrvError(422, "invalid_record_key", "Pemeriksaan metodologi menggunakan satu catatan berversi aktif.")
    if category == "satellite_provenance" and key != str(data.payload.get("snapshot_id", "")):
        raise DmrvError(422, "snapshot_key_mismatch", "Kunci catatan provenance harus sama dengan ID snapshot.")
    if (category in {"risk", "grievance"}) != (data.status == "open"):
        raise DmrvError(
            422, "invalid_status", "Risiko baru harus terbuka; dokumen harus draft atau diajukan."
        )
    try:
        validated = SCHEMAS[category].model_validate(data.payload)
    except ValidationError as exc:
        raise DmrvError(422, "invalid_input", str(exc)) from None
    if category == "methodology_check" and data.status == "submitted" and any(
        item.status == "not_assessed" for item in validated.items
    ):
        raise DmrvError(422, "methodology_check_incomplete", "Nilai atau tandai tidak berlaku semua butir sebelum mengajukan review.")
    evidence_refs = _evidence_references(validated)
    _documents_exist(db, project_id, evidence_refs)
    if data.status == "submitted" and not evidence_refs:
        raise DmrvError(422, "evidence_required", "Lampirkan bukti sebelum mengajukan penilaian.")
    if data.status == "submitted" and category in {"safeguard_metric", "community_benefit"}:
        field_validation = _field_capture_evidence(db, project_id, validated.field_capture_ids, validated.period)
        if not field_validation["capture_count"]:
            raise DmrvError(422, "field_validation_required", "Tambahkan catatan lapangan berkoordinat dan bertanggal sebelum mengajukan review safeguards/manfaat.")
        if not field_validation["qa_ready"]:
            raise DmrvError(422, "field_validation_qa_failed", "Tuntaskan flag mutu GPS/batas pada catatan lapangan sebelum mengajukan review.")
        if category == "community_benefit" and not validated.consent_reference.strip():
            raise DmrvError(422, "community_consent_reference_required", "Referensi persetujuan masyarakat diperlukan sebelum catatan manfaat diajukan.")
    row = db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == project_id,
            DmrvIntegrityRecord.category == category,
            DmrvIntegrityRecord.record_key == key,
        )
    ).scalar_one_or_none()
    if (row.revision if row else 0) != expected:
        raise DmrvError(412, "revision_mismatch", "Penilaian berubah; muat ulang sebelum menyimpan.")
    result = _calculated(db, project_id, category, validated)
    payload = validated.model_dump(mode="json")
    before = out(row) if row else None
    moment = dt.datetime.now(dt.UTC)
    try:
        if row is None:
            row = DmrvIntegrityRecord(
                id=str(uuid.uuid4()),
                project_id=project_id,
                category=category,
                record_key=key,
                title=data.title,
                payload=payload,
                result=result,
                status=data.status,
                revision=1,
                created_by=principal.key,
                updated_by=principal.key,
                created_at=moment,
                updated_at=moment,
            )
            db.add(row)
            db.flush()
        else:
            changed = db.execute(
                update(DmrvIntegrityRecord)
                .where(DmrvIntegrityRecord.id == row.id, DmrvIntegrityRecord.revision == expected)
                .values(
                    title=data.title,
                    payload=payload,
                    result=result,
                    status=data.status,
                    revision=expected + 1,
                    updated_by=principal.key,
                    updated_at=moment,
                    reviewed_by=None,
                    review_reason=None,
                )
            )
            if changed.rowcount != 1:
                raise DmrvError(412, "revision_mismatch", "Penilaian berubah; muat ulang.")
            db.refresh(row)
        retain_revision(db, row, principal, before, request_id=request_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DmrvError(409, "conflict", "Penilaian dibuat bersamaan; muat ulang.") from None
    except Exception:
        db.rollback()
        raise
    return out(row)


def review_record(
    db: Session,
    principal: Principal,
    project_id: str,
    record_id: str,
    data: RecordReview,
    expected: int,
    request_id: str | None = None,
) -> dict:
    authorize(db, project_id, principal, "accounting.read")
    row = _record(db, project_id, record_id)
    if row.category in {"risk", "grievance"}:
        authorize(db, project_id, principal, "monitoring.write")
        if data.decision not in {"acknowledged", "resolved"} or row.status not in {"open", "acknowledged"}:
            raise DmrvError(409, "invalid_transition", "Risiko harus terbuka untuk ditindaklanjuti.")
    else:
        authorize(db, project_id, principal, "review.decide")
        if principal.key in {row.created_by, row.updated_by}:
            raise DmrvError(
                403, "independent_reviewer_required", "Penulis tidak dapat menelaah penilaiannya sendiri."
            )
        if row.status != "submitted" or data.decision not in {"accepted", "rejected", "not_applicable"}:
            raise DmrvError(409, "invalid_transition", "Hanya penilaian yang diajukan yang dapat diputuskan.")
        if data.decision == "not_applicable" and not any(
            c["category"] == row.category and c["key"] == row.record_key and not c["required"]
            for c in CATALOG
        ):
            raise DmrvError(422, "required_criterion", "Butir wajib tidak dapat dikecualikan.")
        if data.decision == "accepted" and row.category == "accounting" and row.result.get("missing_inputs"):
            raise DmrvError(422, "incomplete_accounting", "Lengkapi input perhitungan sebelum diterima.")
    if row.revision != expected:
        raise DmrvError(412, "revision_mismatch", "Penilaian berubah; muat ulang.")
    before = out(row)
    moment = dt.datetime.now(dt.UTC)
    next_result = dict(row.result or {})
    if row.category in {"safeguard_metric", "community_benefit"} and data.decision == "accepted":
        field_validation = (next_result.get("field_validation") or {})
        if not field_validation.get("capture_count") or not field_validation.get("qa_ready"):
            raise DmrvError(422, "field_validation_required", "Review safeguards/manfaat memerlukan bukti lapangan dengan QA GPS/batas yang lolos.")
        next_result["review_accepted_for_reporting"] = True
        next_result["field_validation_reviewed_at"] = dt.datetime.now(dt.UTC).isoformat()
        if row.category == "community_benefit":
            delivered = row.payload.get("status") == "disbursed" and row.payload.get("recipient_acknowledged") is True and bool(row.payload.get("delivery_reference"))
            next_result["verified_for_reporting"] = delivered
            if not delivered:
                next_result["verification_note"] = "Reviewer accepted the record; delivery is not verified until disbursement evidence and recipient acknowledgement are recorded."
        else:
            next_result["verified_for_reporting"] = True
    if row.category == "grievance":
        try:
            received = dt.date.fromisoformat(str(row.payload.get("received_on", "")))
            elapsed = max(0, (moment.date() - received).days)
            next_result["days_open_at_last_update"] = elapsed
            if data.decision == "resolved":
                next_result["resolution_days"] = elapsed
                next_result["resolved_at"] = moment.isoformat()
        except ValueError:
            next_result["resolution_timing_status"] = "received_date_unavailable"
    try:
        changed = db.execute(
            update(DmrvIntegrityRecord)
            .where(DmrvIntegrityRecord.id == row.id, DmrvIntegrityRecord.revision == expected)
            .values(
                status=data.decision,
                result=next_result,
                revision=expected + 1,
                reviewed_by=principal.key,
                review_reason=data.reason,
                updated_at=moment,
            )
        )
        if changed.rowcount != 1:
            raise DmrvError(412, "revision_mismatch", "Penilaian berubah; muat ulang.")
        db.refresh(row)
        retain_revision(db, row, principal, before, data.reason, request_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return out(row)


def readiness(records) -> dict:
    lookup = {(r.category, r.record_key): r.status for r in records}
    criteria = [c for c in CATALOG if lookup.get((c["category"], c["key"])) != "not_applicable"]
    accepted = sum(lookup.get((c["category"], c["key"])) == "accepted" for c in criteria)
    submitted = sum(lookup.get((c["category"], c["key"])) == "submitted" for c in criteria)
    blockers = [
        {**c, "status": lookup.get((c["category"], c["key"]), "missing")}
        for c in criteria
        if lookup.get((c["category"], c["key"])) != "accepted"
    ]
    return {
        "score_pct": round(100 * accepted / len(criteria), 1) if criteria else 0,
        "accepted": accepted,
        "submitted": submitted,
        "total": len(criteria),
        "blockers": blockers,
        "qualification": "Internal evidence readiness only; eligibility and certification are separate decisions.",
    }


def workspace(db: Session, principal: Principal, project_id: str, *, full: bool = False) -> dict:
    authorize(db, project_id, principal, "accounting.read")
    model = DmrvIntegrityRecord
    if full:
        rows = (
            db.execute(select(model).where(model.project_id == project_id).order_by(model.updated_at.desc()))
            .scalars()
            .all()
        )
        records = [out(row) for row in rows]
        summary_rows = rows
    else:
        # Exclude large observation arrays in SQL, before transfer or Python allocation.
        # Full inputs remain available in the record/history and the review export.
        if db.bind.dialect.name == "postgresql":
            payload = model.payload.op("-")("pairs").op("-")("plots").op("-")("rows")
            result = model.result.op("-")("oof").op("-")("rows")
        else:
            payload = func.json_remove(model.payload, "$.pairs", "$.plots", "$.rows")
            result = func.json_remove(model.result, "$.oof", "$.rows")
        columns = [column for column in model.__table__.columns if column.name not in {"payload", "result"}]
        query = (
            select(
                *columns,
                payload.label("payload"),
                result.label("result"),
            )
            .where(model.project_id == project_id)
            .order_by(model.updated_at.desc())
        )
        summary_rows = db.execute(query).all()
        # SQLite's json_remove() returns JSON text, while PostgreSQL JSONB
        # expressions are decoded by the driver. Normalize both paths before
        # Pydantic validation and before registry/readiness summaries inspect
        # payload/result fields.
        normalized_rows = []
        for row in summary_rows:
            values = dict(row._mapping)
            for field in ("payload", "result"):
                if isinstance(values.get(field), str):
                    values[field] = json.loads(values[field])
            normalized_rows.append(RecordOut.model_validate(values))
        summary_rows = normalized_rows
        records = [row.model_dump(mode="json") for row in summary_rows]
    documents = (
        db.execute(
            select(DmrvIntegrityDocument)
            .where(DmrvIntegrityDocument.project_id == project_id)
            .order_by(DmrvIntegrityDocument.created_at.desc())
        )
        .scalars()
        .all()
    )
    policy = db.get(DmrvRiskPolicy, project_id)
    return {
        "project_id": project_id,
        "catalog": CATALOG,
        "readiness": readiness(summary_rows),
        "records": records,
        "documents": [document_out(d) for d in documents],
        "risk_policy": policy_out(policy),
        "registry_check": registry_check(db, project_id, summary_rows),
    }


def policy_out(policy: DmrvRiskPolicy | None) -> dict:
    return (
        {
            "enabled": policy.enabled,
            "interval_days": policy.interval_days,
            "lookback_days": policy.lookback_days,
            "tree_loss_threshold_ha": policy.tree_loss_threshold_ha,
            "min_observations": policy.min_observations,
            "revision": policy.revision,
            "next_run_at": policy.next_run_at,
            "last_enqueued_at": policy.last_enqueued_at,
        }
        if policy
        else {
            "enabled": False,
            "interval_days": 7,
            "lookback_days": 30,
            "tree_loss_threshold_ha": 1,
            "min_observations": 3,
            "revision": 0,
            "next_run_at": None,
            "last_enqueued_at": None,
        }
    )


def document_out(document: DmrvIntegrityDocument) -> dict:
    return {
        key: getattr(document, key)
        for key in ("id", "filename", "content_type", "sha256", "size_bytes", "created_at")
    }


def registry_check(db: Session, project_id: str, records=None) -> dict:
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from app.services.dmrv.boundary import _area_m2
    from app.services.dmrv.projects import _other_projects_latest_geometries

    own = (
        records
        if records is not None
        else db.execute(
            select(DmrvIntegrityRecord).where(
                DmrvIntegrityRecord.project_id == project_id, DmrvIntegrityRecord.category == "registry"
            )
        )
        .scalars()
        .all()
    )
    own = [r for r in own if r.category == "registry" and r.status != "rejected"]
    # Only conflict counts leave this service; other projects' data are never returned.
    other_payloads = (
        db.execute(
            select(DmrvIntegrityRecord.payload).where(
                DmrvIntegrityRecord.category == "registry",
                DmrvIntegrityRecord.project_id != project_id,
                DmrvIntegrityRecord.status != "rejected",
            )
        )
        .scalars()
        .all()
    )
    conflicts = []
    for record in own:
        p = record.payload
        candidates = other_payloads + [r.payload for r in own if r.id != record.id]
        count = sum(
            str(p["registry"]).strip().casefold() == str(q["registry"]).strip().casefold()
            and str(p["serial_prefix"]).strip().casefold() == str(q["serial_prefix"]).strip().casefold()
            and p["serial_start"] <= q["serial_end"]
            and q["serial_start"] <= p["serial_end"]
            for q in candidates
        )
        if count:
            conflicts.append({"record_id": record.id, "conflicting_records": count})
    boundary = db.execute(
        select(BoundaryVersion)
        .where(BoundaryVersion.project_id == project_id)
        .order_by(BoundaryVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    overlaps = []
    if boundary:
        base = shape(boundary.validated_geojson)
        for geom in _other_projects_latest_geometries(db, project_id):
            intersection = base.intersection(shape(geom))
            if _area_m2(intersection) > 1:
                overlaps.append(intersection)
    return {
        "serial_conflicts": conflicts,
        "overlapping_projects": len(overlaps),
        "unique_overlap_ha": _area_m2(unary_union(overlaps)) / 10000 if overlaps else 0,
        "boundary_version": boundary.version if boundary else None,
        "external_registry_status": "not_connected",
        "qualification": "Internal records and geometry screening; overlap alone does not establish double counting. External registry confirmation is required.",
    }


def portfolio(
    db: Session,
    principal: Principal,
    search: str = "",
    organization: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    query = (
        select(DmrvProject, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == DmrvProject.id)
        .where(
            ProjectMember.principal_type == principal.type,
            ProjectMember.principal_id == principal.id,
            ProjectMember.role.in_(["project_owner", "analyst", "reviewer", "verifier"]),
            or_(
                DmrvProject.organization_id.is_(None),
                exists().where(
                    OrganizationMember.organization_id == DmrvProject.organization_id,
                    OrganizationMember.principal_type == principal.type,
                    OrganizationMember.principal_id == principal.id,
                    OrganizationMember.is_active.is_(True),
                ),
            ),
        )
    )
    if search:
        query = query.where(DmrvProject.name.icontains(search, autoescape=True))
    if organization:
        query = query.where(DmrvProject.organization == organization)
    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    page = db.execute(
        query.order_by(DmrvProject.created_at.desc(), DmrvProject.id).offset(offset).limit(limit)
    ).all()
    ids = [project.id for project, _ in page]
    if not ids:
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [],
            "summary": {"projects": 0, "open_risks": 0, "overdue_periods": 0},
        }
    # Bounded bulk reads: no query per project and no validation input arrays loaded here.
    criteria_rows = db.execute(
        select(
            DmrvIntegrityRecord.project_id,
            DmrvIntegrityRecord.category,
            DmrvIntegrityRecord.record_key,
            DmrvIntegrityRecord.status,
        ).where(DmrvIntegrityRecord.project_id.in_(ids))
    ).all()
    grouped = defaultdict(list)
    for row in criteria_rows:
        grouped[row.project_id].append(row)
    bounds = db.execute(
        select(BoundaryVersion.project_id, BoundaryVersion.version, BoundaryVersion.area_ha)
        .where(BoundaryVersion.project_id.in_(ids))
        .order_by(BoundaryVersion.version.desc())
    ).all()
    areas: dict[int, float] = {}
    for row in bounds:
        areas.setdefault(row.project_id, row.area_ha)
    today = dt.datetime.now(dt.UTC).date()
    overdue = dict(
        db.execute(
            select(DmrvMonitoringPeriod.project_id, func.count())
            .where(
                DmrvMonitoringPeriod.project_id.in_(ids),
                DmrvMonitoringPeriod.end_date < today,
                DmrvMonitoringPeriod.status != "completed",
            )
            .group_by(DmrvMonitoringPeriod.project_id)
        ).all()
    )
    plots = dict(
        db.execute(
            select(SurveyPlot.project_id, func.count())
            .where(SurveyPlot.project_id.in_(ids))
            .group_by(SurveyPlot.project_id)
        ).all()
    )
    packages = dict(
        db.execute(
            select(DmrvEvidencePackage.project_id, func.count())
            .where(DmrvEvidencePackage.project_id.in_(ids))
            .group_by(DmrvEvidencePackage.project_id)
        ).all()
    )
    findings = dict(
        db.execute(
            select(DmrvSubmission.project_id, func.count())
            .join(DmrvReviewFinding, DmrvReviewFinding.submission_id == DmrvSubmission.id)
            .where(DmrvSubmission.project_id.in_(ids), DmrvReviewFinding.status != "resolved")
            .group_by(DmrvSubmission.project_id)
        ).all()
    )
    items = []
    for project, role in page:
        rows = grouped[project.id]
        score = readiness(rows)
        risks = sum(r.category == "risk" and r.status in {"open", "acknowledged"} for r in rows)
        gaps = []
        if project.id not in areas:
            gaps.append("boundary")
        if not project.methodology_id or not project.methodology_version:
            gaps.append("methodology_version")
        if not plots.get(project.id):
            gaps.append("field_data")
        if not packages.get(project.id):
            gaps.append("evidence_package")
        items.append(
            {
                "id": project.id,
                "name": project.name,
                "organization": project.organization,
                "activity_type": project.activity_type,
                "methodology_id": project.methodology_id,
                "methodology_version": project.methodology_version,
                "my_role": role,
                "area_ha": areas.get(project.id),
                "readiness": score,
                "open_risks": risks,
                "overdue_periods": overdue.get(project.id, 0),
                "open_findings": findings.get(project.id, 0),
                "data_gaps": gaps,
            }
        )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
        "summary": {
            "projects": len(items),
            "open_risks": sum(i["open_risks"] for i in items),
            "overdue_periods": sum(i["overdue_periods"] for i in items),
            "scope": "current_page",
        },
    }
