"""The seven integrity workflows, under /api/dmrv; no admin bypass."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.core.security import get_current_app_viewer
from app.db.models.dmrv_advanced import DmrvCreditEvent, DmrvFieldCapture
from app.db.models.dmrv_field import SurveyPlot
from app.db.models.dmrv_integrity import DmrvIntegrityDocument, DmrvIntegrityRevision
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.dmrv_job import DmrvSnapshot
from app.db.models.user import User
from app.db.models.admin_user import AdminUser
from app.db.session import get_db
from app.schemas.dmrv_integrity import (
    CreditEventInput, FieldCaptureInput, RecordOut, RecordReview, RecordSave,
    RiskPolicyInput, RiskScanInput,
)
from app.schemas.dmrv_carbon_features import RegistryReconciliationInput, RegistrySyncInput
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv import integrity
from app.services.dmrv import registry_connector
from app.services.dmrv.integrity_risks import enqueue_scan, save_policy
from app.services.dmrv import advanced, methodology, risk_priority

router = APIRouter()


def _snapshot_quality(inputs: dict, outputs: dict) -> dict:
    aliases = {
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
        "aerosol_qa": {"aerosol_qa", "aerosol_method", "aerosol_correction", "atmospheric_correction", "aerosol_handling", "aot_band_available_in_source_product"},
        "observation_start": {"observation_start", "start_date", "date_start"},
        "observation_end": {"observation_end", "end_date", "date_end", "end"},
        "season_window": {"season", "season_window", "seasonal_window"},
        "provider_scene_manifest": {"source_provenance", "provider_scene_manifest", "scene_manifest"},
    }
    found = {}
    queue = [{**(inputs or {}), **(outputs or {})}]
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
                    if normalized in aliases["provider_scene_manifest"] and "provider_scene_manifest" not in found:
                        found["provider_scene_manifest"] = value[:100]
                    queue.extend(value[:100])
                elif isinstance(value, (str, int, float, bool)):
                    for name, names in aliases.items():
                        if normalized in names and name not in found:
                            found[name] = value
    if found.get("start_month") is not None and found.get("end_month") is not None:
        found["season_window"] = f"months {found['start_month']}-{found['end_month']}"
    warnings = []
    if "cloud_cover_pct" not in found:
        warnings.append("cloud_cover_not_available_in_snapshot")
    if "spatial_resolution_m" not in found:
        warnings.append("spatial_resolution_not_available_in_snapshot")
    if "observation_count" not in found:
        warnings.append("observation_count_not_available_in_snapshot")
    if "aerosol_qa" not in found:
        warnings.append("aerosol_qa_not_available_in_snapshot")
    if not found.get("provider_scene_manifest"):
        warnings.append("scene_level_acquisition_ids_not_retained_by_this_snapshot")
    if isinstance(found.get("cloud_cover_pct"), (float, int)) and found["cloud_cover_pct"] > 30:
        warnings.append("high_cloud_cover_review_recommended")
    if isinstance(found.get("observation_count"), (float, int)) and found["observation_count"] < 3:
        warnings.append("few_observations_review_recommended")
    if found.get("gap_filled") is True:
        warnings.append("temporal_gap_filling_in_analysis_review_recommended")
    if found.get("analysis_year_partial") is True:
        warnings.append("current_year_partial_observation_window")
    if isinstance(found.get("valid_aoi_pixel_pct"), (float, int)) and found["valid_aoi_pixel_pct"] < 70:
        warnings.append("low_valid_aoi_pixel_coverage_review_recommended")
    captured_live = bool(found.get("provider_scene_manifest"))
    return {"metadata": found, "quality_warnings": warnings,
            "attestation": "provider_collection_metadata_captured_during_analysis_and_retained_in_immutable_snapshot" if captured_live else "extracted_from_immutable_savegeo_snapshot_not_provider_verified"}


@router.get("/projects/{project_id}/integrity/image-provenance/snapshots")
def list_image_provenance_snapshots(
    project_id: str, limit: int = Query(default=50, ge=1, le=100),
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "accounting.read")
    rows = db.execute(
        select(DmrvSnapshot).where(
            DmrvSnapshot.project_id == project_id,
            DmrvSnapshot.kind.in_(["carbon", "vegetation", "landcover", "landcover_transition"]),
        )
        .order_by(DmrvSnapshot.created_at.desc()).limit(limit)
    ).scalars().all()
    return {"items": [{
        "id": row.id, "kind": row.kind, "snapshot_hash": row.snapshot_hash,
        "created_at": row.created_at.isoformat(),
        **_snapshot_quality(row.inputs, row.outputs),
    } for row in rows]}


@router.post("/projects/{project_id}/integrity/registry-import", status_code=201)
async def import_registry_csv(
    project_id: str, request: Request, registry: str = Form(...), key: str = Form(...),
    source_url: str = Form(default=""), file: UploadFile = File(...),
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "monitoring.write")
    if Path(file.filename or "").suffix.lower() != ".csv":
        raise DmrvError(422, "registry_csv_required", "Unggah file CSV dari registry.")
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise DmrvError(413, "registry_csv_too_large", "CSV registry maksimal 5 MB.")
    try:
        content = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        required = {"external_project_id", "vintage", "serial", "event_type", "event_date", "quantity", "account", "transaction_id"}
        headers = {str(item).strip().lower() for item in (reader.fieldnames or [])}
        if not required.issubset(headers):
            raise ValueError("Header wajib: " + ", ".join(sorted(required)))
        rows = []
        for line, row in enumerate(reader, start=2):
            if len(rows) >= 1000:
                raise ValueError("CSV maksimal berisi 1.000 baris.")
            normalized = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
            rows.append({
                "external_project_id": normalized["external_project_id"],
                "vintage": int(normalized["vintage"]), "serial": normalized["serial"],
                "event_type": normalized["event_type"].lower(), "event_date": normalized["event_date"],
                "quantity": int(normalized["quantity"]), "account": normalized["account"],
                "transaction_id": normalized["transaction_id"],
            })
        imported = RegistryReconciliationInput.model_validate({
            "registry": registry, "source_url": source_url or None,
            "source_file_sha256": hashlib.sha256(raw).hexdigest(), "rows": rows,
            "evidence": [],
        })
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise DmrvError(422, "invalid_registry_csv", f"CSV tidak valid: {exc}") from None
    total = db.execute(
        select(func.coalesce(func.sum(DmrvIntegrityDocument.size_bytes), 0)).where(
            DmrvIntegrityDocument.project_id == project_id
        )
    ).scalar_one()
    if total + len(raw) > 50 * 1024 * 1024:
        raise DmrvError(413, "project_document_limit", "Batas dokumen proyek 50 MB tercapai.")
    document = DmrvIntegrityDocument(
        id=str(uuid.uuid4()), project_id=project_id,
        filename=Path(file.filename or "registry-import.csv").name[:200],
        content_type="text/csv", sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw), created_by=principal.key,
    )
    target = integrity.document_path(document)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        db.add(document)
        db.flush()
        record_audit(
            db, principal=principal, action="integrity.document.upload",
            resource_type="integrity_document", resource_id=document.id,
            project_id=project_id,
            after={"filename": document.filename, "sha256": document.sha256, "size_bytes": document.size_bytes},
        )
        evidence = [f"document:{document.id}"]
        if imported.source_url:
            evidence.append(str(imported.source_url))
        imported = RegistryReconciliationInput.model_validate({
            **imported.model_dump(mode="json"), "evidence": evidence,
        })
        payload = RecordSave(
            title=f"Registry reconciliation · {registry}",
            payload=imported.model_dump(mode="json"), status="draft",
        )
        return integrity.save_record(
            db, principal, project_id, "registry_import", key, payload,
            revision(if_match), getattr(request.state, "request_id", None),
        )
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise


@router.get("/projects/{project_id}/integrity/registry-connector/status")
def get_registry_connector_status(
    project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    authorize(db, project_id, Principal.from_account(viewer), "accounting.read")
    return registry_connector.status()


@router.post("/projects/{project_id}/integrity/registry-connector/sync", status_code=201)
def sync_official_registry(
    project_id: str, payload: RegistrySyncInput, request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "monitoring.write")
    imported = registry_connector.fetch(payload.registry, payload.external_project_id)
    return integrity.save_record(
        db, principal, project_id, "registry_import", payload.record_key,
        RecordSave(
            title=f"Official registry sync · {payload.registry} · {payload.external_project_id}",
            payload=imported.model_dump(mode="json"), status="draft",
        ),
        revision(if_match), getattr(request.state, "request_id", None),
    )


@router.get("/projects/{project_id}/integrity/methodology-checklist")
def get_methodology_checklist(
    project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    return methodology.get_checklist(db, Principal.from_account(viewer), project_id)


@router.get("/projects/{project_id}/integrity/risk-priority")
def get_risk_priority(
    project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    return risk_priority.reviewer_priority(db, Principal.from_account(viewer), project_id)


@router.get("/projects/{project_id}/integrity/credit-events")
def get_credit_events(
    project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    return advanced.list_credit_events(db, Principal.from_account(viewer), project_id)


@router.post("/projects/{project_id}/integrity/credit-events", status_code=201)
def post_credit_event(
    project_id: str, payload: CreditEventInput, response: Response,
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    existing = db.get(DmrvCreditEvent, payload.event_id)
    result = advanced.append_credit_event(db, Principal.from_account(viewer), project_id, payload)
    if existing:
        response.status_code = 200
    return result


@router.get("/projects/{project_id}/field-data/captures")
def get_field_captures(
    project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    return advanced.list_field_captures(db, Principal.from_account(viewer), project_id)


@router.post("/projects/{project_id}/field-data/captures", status_code=201)
async def post_field_capture(
    project_id: str, response: Response, data: str = Form(...), file: UploadFile | None = File(default=None),
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    try:
        payload = FieldCaptureInput.model_validate(json.loads(data))
    except (ValueError, TypeError) as exc:
        raise DmrvError(422, "invalid_field_capture", str(exc)) from None
    existing = db.get(DmrvFieldCapture, payload.capture_id)
    raw = await file.read(5 * 1024 * 1024 + 1) if file else None
    if raw is not None and len(raw) > 5 * 1024 * 1024:
        raise DmrvError(413, "photo_size", "Foto lapangan maksimal 5 MB.")
    result = advanced.capture_field_observation(
        db, Principal.from_account(viewer), project_id, payload, raw, file.filename if file else None
    )
    if existing:
        response.status_code = 200
    return result


@router.get("/projects/{project_id}/field-data/captures/{capture_id}/photo")
def get_field_capture_photo(
    project_id: str, capture_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "field.read")
    capture = db.get(DmrvFieldCapture, capture_id)
    if capture is None or capture.project_id != project_id or not capture.photo_document_id:
        raise DmrvError(404, "not_found", "Foto catatan lapangan tidak ditemukan.")
    document = db.get(DmrvIntegrityDocument, capture.photo_document_id)
    if document is None or document.project_id != project_id:
        raise DmrvError(404, "not_found", "Foto catatan lapangan tidak ditemukan.")
    path = integrity.document_path(document)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != document.sha256:
        raise DmrvError(409, "document_integrity_failed", "Foto hilang atau checksum tidak cocok.")
    return FileResponse(path, filename=document.filename, media_type=document.content_type,
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/projects/{project_id}/integrity/members")
def list_members(project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    authorize(db, project_id, Principal.from_account(viewer), "members.manage")
    rows = db.execute(select(ProjectMember).where(ProjectMember.project_id == project_id)).scalars().all()
    output = []
    for row in rows:
        account = db.get(User if row.principal_type == "user" else AdminUser, row.principal_id)
        output.append(
            {
                "principal_type": row.principal_type,
                "principal_id": row.principal_id,
                "username": account.username if account else "",
                "role": row.role,
            }
        )
    return output


@router.delete("/projects/{project_id}/integrity/members/{principal_type}/{principal_id}")
def revoke_member(
    project_id: str,
    principal_type: str,
    principal_id: int,
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "members.manage")
    if (principal.type, principal.id) == (principal_type, principal_id):
        raise DmrvError(422, "self_revoke", "Pemilik tidak dapat mencabut aksesnya sendiri.")
    row = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_type == principal_type,
            ProjectMember.principal_id == principal_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise DmrvError(404, "not_found", "Anggota tidak ditemukan.")
    try:
        record_audit(
            db,
            principal=principal,
            action="member.revoke",
            resource_type="project_member",
            resource_id=f"{principal_type}:{principal_id}",
            project_id=project_id,
            before={"role": row.role},
        )
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"revoked": True}


def revision(value: str | None) -> int:
    if value is None:
        raise DmrvError(428, "if_match_required", "Header If-Match wajib diisi; gunakan 0 untuk data baru.")
    if not re.fullmatch(r'"?\d+"?', value.strip()):
        raise DmrvError(400, "bad_if_match", "If-Match harus berupa nomor revisi.")
    return int(value.strip().strip('"'))


@router.get("/portfolio")
def get_portfolio(
    search: str = Query(default="", max_length=200),
    organization: str = Query(default="", max_length=255),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return integrity.portfolio(db, Principal.from_account(viewer), search, organization, limit, offset)


@router.get("/projects/{project_id}/integrity")
def get_workspace(project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return integrity.workspace(db, Principal.from_account(viewer), project_id)


@router.put("/projects/{project_id}/integrity/records/{category}/{key}", response_model=RecordOut)
def put_record(
    project_id: str,
    category: str,
    key: str,
    payload: RecordSave,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return integrity.save_record(
        db,
        Principal.from_account(viewer),
        project_id,
        category,
        key,
        payload,
        revision(if_match),
        getattr(request.state, "request_id", None),
    )


@router.post("/projects/{project_id}/integrity/records/{record_id}/review", response_model=RecordOut)
def review(
    project_id: str,
    record_id: str,
    payload: RecordReview,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return integrity.review_record(
        db,
        Principal.from_account(viewer),
        project_id,
        record_id,
        payload,
        revision(if_match),
        getattr(request.state, "request_id", None),
    )


@router.get("/projects/{project_id}/integrity/records/{record_id}", response_model=RecordOut)
def get_record(
    project_id: str, record_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    authorize(db, project_id, Principal.from_account(viewer), "accounting.read")
    return integrity.out(integrity._record(db, project_id, record_id))


@router.post("/projects/{project_id}/integrity/records/{record_id}/deploy-plots")
def deploy_sampling_plan_plots(
    project_id: str, record_id: str, request: Request,
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "field.write")
    record = integrity._record(db, project_id, record_id)
    if record.category != "sampling_plan":
        raise DmrvError(422, "sampling_plan_required", "Catatan ini bukan rencana sampel lapangan.")
    if record.status != "accepted":
        raise DmrvError(409, "sampling_plan_review_required", "Ajukan dan dapatkan persetujuan reviewer independen sebelum membuat plot lapangan.")
    deployment = (record.result or {}).get("field_plot_deployment")
    if deployment:
        return deployment
    planned = (record.result or {}).get("strata", [])
    expected_plot_count = int((record.result or {}).get("recommended_plot_count", 0))
    locations = []
    plan_tag = re.sub(r"[^A-Za-z0-9]", "", record.id)[:8].upper()
    for stratum in planned:
        stratum_tag = re.sub(r"[^A-Za-z0-9]+", "-", str(stratum.get("stratum", "STRATUM"))).strip("-").upper()[:16] or "STRATUM"
        for index, point in enumerate(stratum.get("plot_locations") or [], start=1):
            locations.append({"plot_code": f"SG-{plan_tag}-{stratum_tag}-{index:04d}"[:64],
                              "stratum": stratum.get("stratum"), "lat": point["lat"], "lon": point["lon"]})
    if not locations:
        raise DmrvError(422, "sample_coordinates_required", "Rencana tidak mempunyai koordinat plot. Tambahkan poligon GeoJSON strata dan hitung ulang.")
    if expected_plot_count > 500:
        raise DmrvError(422, "sample_plan_too_large", "Deploy maksimal 500 plot per rencana; pecah kampanye lapangan.")
    if len(locations) != expected_plot_count:
        raise DmrvError(422, "sample_plan_coordinates_incomplete", "Koordinat yang tersimpan tidak mencakup semua plot rencana; hitung ulang dengan geometri strata.")
    plan_boundary_version = ((record.result or {}).get("project_context") or {}).get("boundary_version")
    boundary_query = select(BoundaryVersion).where(BoundaryVersion.project_id == project_id)
    if plan_boundary_version is not None:
        boundary_query = boundary_query.where(BoundaryVersion.version == plan_boundary_version)
    boundary = db.execute(boundary_query.order_by(BoundaryVersion.version.desc()).limit(1)).scalar_one_or_none()
    if boundary is None:
        raise DmrvError(422, "project_boundary_required", "Batas proyek tervalidasi diperlukan sebelum plot dibuat.")
    latest_version = db.execute(select(func.max(BoundaryVersion.version)).where(BoundaryVersion.project_id == project_id)).scalar_one()
    if plan_boundary_version is not None and int(latest_version or 0) != int(plan_boundary_version):
        raise DmrvError(409, "sampling_boundary_changed", "Batas proyek berubah setelah rencana sampel dibuat. Hitung ulang rencana dengan batas terbaru.")
    from shapely.geometry import Point, shape
    project_shape = shape(boundary.validated_geojson)
    if not project_shape.is_valid:
        raise DmrvError(409, "invalid_project_boundary", "Geometri batas proyek tersimpan tidak valid.")
    if any(not project_shape.covers(Point(item["lon"], item["lat"])) for item in locations):
        raise DmrvError(422, "plot_outside_project_boundary", "Satu atau lebih koordinat rekomendasi berada di luar batas proyek.")
    area_m2 = float(record.payload.get("plot_area_m2", 400))
    codes = [item["plot_code"] for item in locations]
    if len(set(codes)) != len(codes):
        raise DmrvError(409, "duplicate_sample_plot_code", "Rencana menghasilkan kode plot berulang.")
    existing = db.execute(select(SurveyPlot).where(
        SurveyPlot.project_id == project_id, SurveyPlot.plot_code.in_(codes),
    )).scalars().all()
    by_code = {row.plot_code: row for row in existing}
    for item in locations:
        prior = by_code.get(item["plot_code"])
        if prior and (prior.latitude is None or prior.longitude is None or
                      abs(prior.latitude - item["lat"]) > 1e-6 or abs(prior.longitude - item["lon"]) > 1e-6 or
                      prior.area_m2 != area_m2):
            raise DmrvError(409, "sample_plot_code_conflict", f"Kode plot {item['plot_code']} sudah dipakai untuk plot lain.")
    created = []
    for item in locations:
        if item["plot_code"] in by_code:
            continue
        row = SurveyPlot(
            id=str(uuid.uuid4()), project_id=project_id, plot_code=item["plot_code"],
            latitude=item["lat"], longitude=item["lon"], area_m2=area_m2, area_basis="assumed",
        )
        db.add(row)
        created.append(item["plot_code"])
    before = integrity.out(record)
    deployment = {
        "sampling_plan_record_id": record.id,
        "created_plot_codes": created,
        "existing_plot_codes_reused": [item["plot_code"] for item in locations if item["plot_code"] in by_code],
        "plot_count": len(locations), "plot_area_m2_assumed": area_m2,
        "boundary_version": boundary.version, "boundary_checksum": boundary.checksum,
        "deployed_at": dt.datetime.now(dt.UTC).isoformat(),
        "measurement_workflow": "Plots are now in the field register; capture GPS/photo observations or import repeated tree measurements using these plot codes.",
        "warnings": ["plot_area_is_assumed_until_measured_in_field", "coordinates_are_sampling_recommendations_not_completed_measurements"],
    }
    record.result = {**(record.result or {}), "field_plot_deployment": deployment}
    record.revision += 1
    record.updated_by = principal.key
    record.updated_at = dt.datetime.now(dt.UTC)
    db.flush()
    integrity.retain_revision(db, record, principal, before, "Accepted sampling plan deployed to the project field-plot register.", getattr(request.state, "request_id", None))
    record_audit(db, principal=principal, action="field.sampling_plan.deploy", resource_type="integrity_record",
                 resource_id=record.id, project_id=project_id,
                 after={"plot_count": len(locations), "created_count": len(created),
                        "boundary_version": boundary.version, "boundary_checksum": boundary.checksum},
                 request_id=getattr(request.state, "request_id", None))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return deployment


@router.get("/projects/{project_id}/integrity/records/{record_id}/field-template.csv")
def sampling_plan_field_template(
    project_id: str, record_id: str,
    viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db),
):
    authorize(db, project_id, Principal.from_account(viewer), "field.write")
    record = integrity._record(db, project_id, record_id)
    if record.category != "sampling_plan" or record.status != "accepted":
        raise DmrvError(409, "accepted_sampling_plan_required", "Template hanya tersedia untuk rencana sampel yang sudah diterima reviewer.")
    deployment = (record.result or {}).get("field_plot_deployment")
    if not deployment:
        raise DmrvError(409, "sampling_plan_not_deployed", "Buat plot di register lapangan sebelum mengunduh template ukur.")
    codes = list(dict.fromkeys((deployment.get("created_plot_codes") or []) + (deployment.get("existing_plot_codes_reused") or [])))
    plots = db.execute(select(SurveyPlot).where(
        SurveyPlot.project_id == project_id, SurveyPlot.plot_code.in_(codes),
    ).order_by(SurveyPlot.plot_code)).scalars().all()
    if len(plots) != len(codes) or not plots:
        raise DmrvError(409, "sampling_plan_plots_missing", "Sebagian plot rencana tidak lagi ada di register lapangan.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["plot_code", "visit_date", "survey_year", "tree_code", "species", "circumference_cm", "dbh_cm", "height", "biomass_kg", "status", "latitude", "longitude", "plot_area_m2", "operator"])
    for plot in plots:
        writer.writerow([plot.plot_code, "", "", "", "", "", "", "", "", "live", "", "", "", ""])
    response = Response(output.getvalue(), media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="savegeo-sampling-plan-{record.id[:8]}.csv"'
    return response


@router.get("/projects/{project_id}/integrity/records/{record_id}/history")
def history(
    project_id: str, record_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    authorize(db, project_id, Principal.from_account(viewer), "accounting.read")
    integrity._record(db, project_id, record_id)
    rows = (
        db.execute(
            select(DmrvIntegrityRevision)
            .where(DmrvIntegrityRevision.record_id == record_id)
            .order_by(DmrvIntegrityRevision.revision.desc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        if hashlib.sha256(integrity.canonical(row.snapshot)).hexdigest() != row.sha256:
            raise DmrvError(409, "revision_integrity_failed", "Checksum riwayat penilaian tidak cocok.")
    return [{"revision": row.revision, "sha256": row.sha256, "snapshot": row.snapshot} for row in rows]


@router.put("/projects/{project_id}/integrity/risk-policy")
def put_policy(
    project_id: str,
    payload: RiskPolicyInput,
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    return save_policy(db, Principal.from_account(viewer), project_id, payload, revision(if_match))


@router.post("/projects/{project_id}/integrity/risk-scans", status_code=202)
def scan(
    project_id: str,
    payload: RiskScanInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    if not idempotency_key or len(idempotency_key) > 100:
        raise DmrvError(400, "idempotency_key_required", "Idempotency-Key wajib diisi (maks. 100 karakter).")
    row = enqueue_scan(db, Principal.from_account(viewer), project_id, payload.end_date, idempotency_key)
    return {"id": row.id, "kind": row.kind, "status": row.status, "project_id": project_id}


@router.post("/projects/{project_id}/integrity/validation-import", response_model=RecordOut)
async def import_validation(
    project_id: str,
    file: UploadFile = File(...),
    metadata: str = Form(...),
    key: str = Form(...),
    if_match: str | None = Header(default=None, alias="If-Match"),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "monitoring.write")
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise DmrvError(413, "file_too_large", "CSV validasi maksimal 2 MB.")
    try:
        payload = json.loads(metadata)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if len(reader.fieldnames or []) != 5 or set(reader.fieldnames or []) != {
            "point_id",
            "lon",
            "lat",
            "observed",
            "predicted",
        }:
            raise ValueError("Kolom CSV: point_id,lon,lat,observed,predicted")
        pairs: list[dict[str, str]] = []
        for line in reader:
            if len(pairs) >= 2000:
                raise ValueError("Maksimal 2000 titik per dataset.")
            pairs.append(line)
        title = payload.pop("title", "Validasi lapangan")
        payload["pairs"] = pairs
        parsed = RecordSave(title=title, payload=payload, status="draft")
    except (ValueError, UnicodeError, TypeError, AttributeError) as exc:
        raise DmrvError(422, "invalid_csv", str(exc)) from None
    return integrity.save_record(db, principal, project_id, "validation", key, parsed, revision(if_match))


@router.post("/projects/{project_id}/integrity/documents", status_code=201)
async def upload_document(
    project_id: str,
    file: UploadFile = File(...),
    viewer=Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "monitoring.write")
    db.execute(select(DmrvProject.id).where(DmrvProject.id == project_id).with_for_update()).first()
    raw = await file.read(5 * 1024 * 1024 + 1)
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise DmrvError(413, "file_size", "Dokumen harus berisi data dan maksimal 5 MB.")
    filename = re.split(r"[/\\]", file.filename or "document")[-1]
    filename = re.sub(r"[^\w .()-]", "_", filename)[:200]
    ext = Path(filename).suffix.lower()
    types = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    valid = ext in types
    if ext == ".pdf":
        valid = raw.startswith(b"%PDF-")
    if ext == ".png":
        valid = raw.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {".jpg", ".jpeg"}:
        valid = raw.startswith(b"\xff\xd8\xff")
    if ext in {".docx", ".xlsx"}:
        valid = zipfile.is_zipfile(io.BytesIO(raw))
    if ext in {".txt", ".csv"}:
        try:
            raw.decode("utf-8-sig")
        except UnicodeError:
            valid = False
    if not valid:
        raise DmrvError(
            422, "unsupported_file", "Gunakan PDF, DOCX, XLSX, UTF-8 TXT/CSV, PNG, atau JPEG yang valid."
        )
    total = db.execute(
        select(func.coalesce(func.sum(DmrvIntegrityDocument.size_bytes), 0)).where(
            DmrvIntegrityDocument.project_id == project_id
        )
    ).scalar_one()
    if total + len(raw) > 50 * 1024 * 1024:
        raise DmrvError(413, "project_document_limit", "Batas dokumen proyek 50 MB tercapai.")
    row = DmrvIntegrityDocument(
        id=str(uuid.uuid4()),
        project_id=project_id,
        filename=filename,
        content_type=types[ext],
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        created_by=principal.key,
    )
    path = integrity.document_path(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(raw)
        db.add(row)
        db.flush()
        record_audit(
            db,
            principal=principal,
            action="integrity.document.upload",
            resource_type="integrity_document",
            resource_id=row.id,
            project_id=project_id,
            after={"filename": filename, "sha256": row.sha256, "size_bytes": len(raw)},
        )
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return integrity.document_out(row)


def _document(db: Session, principal: Principal, project_id: str, document_id: str):
    authorize(db, project_id, principal, "accounting.read")
    row = db.get(DmrvIntegrityDocument, document_id)
    if row is None or row.project_id != project_id:
        raise DmrvError(404, "not_found", "Dokumen tidak ditemukan.")
    path = integrity.document_path(row)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.sha256:
        raise DmrvError(409, "document_integrity_failed", "Dokumen hilang atau checksum tidak cocok.")
    return row, path


@router.get("/projects/{project_id}/integrity/documents/{document_id}/download")
def download_document(
    project_id: str, document_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    row, path = _document(db, Principal.from_account(viewer), project_id, document_id)
    return FileResponse(
        path,
        filename=row.filename,
        media_type=row.content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/projects/{project_id}/integrity/export")
def export_workspace(project_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    principal = Principal.from_account(viewer)
    workspace = jsonable_encoder(integrity.workspace(db, principal, project_id, full=True))
    record_ids = [row["id"] for row in workspace["records"]]
    revisions = (
        db.execute(select(DmrvIntegrityRevision).where(DmrvIntegrityRevision.record_id.in_(record_ids)))
        .scalars()
        .all()
    )
    if any(hashlib.sha256(integrity.canonical(r.snapshot)).hexdigest() != r.sha256 for r in revisions):
        raise DmrvError(409, "revision_integrity_failed", "Checksum riwayat penilaian tidak cocok.")
    package = {
        "format": "savegeo_integrity_workspace_v1",
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "workspace": workspace,
        "field_captures": advanced.list_field_captures(db, principal, project_id, limit=None),
        "credit_ledger": advanced.list_credit_events(db, principal, project_id),
        "history": [
            {"record_id": r.record_id, "revision": r.revision, "sha256": r.sha256, "snapshot": r.snapshot}
            for r in revisions
        ],
        "qualification": "Internal review package; no certification or issuance is implied.",
    }
    temp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    temp.close()
    target = Path(temp.name)
    try:
        manifest = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            metadata = integrity.canonical(package)
            archive.writestr("workspace.json", metadata)
            manifest.append({"path": "workspace.json", "sha256": hashlib.sha256(metadata).hexdigest()})
            for document in workspace["documents"]:
                row, path = _document(db, principal, project_id, document["id"])
                name = f"documents/{row.id}/{row.filename}"
                archive.write(path, name)
                manifest.append({"path": name, "sha256": row.sha256})
            archive.writestr(
                "manifest.json", integrity.canonical({"entries": manifest, "hash_algorithm": "sha256"})
            )
        return FileResponse(
            target,
            filename=f"savegeo-integrity-{project_id}.zip",
            media_type="application/zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise
