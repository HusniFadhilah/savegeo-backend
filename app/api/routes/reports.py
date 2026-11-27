"""Executive summary report (DOCX) - /api/reports/executive-summary. Ported from app.py."""
from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.reporting.executive_summary import build_executive_summary_docx
from app.core.security import get_current_app_viewer
from app.core.bounded_json import read_typed_json_object
from app.core.viewer_features import ensure_feature_access
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.models.analysis_provenance import AnalysisProvenance
from app.db.session import get_db
from app.schemas.reports import ExecutiveSummaryRequest
from app.core.viewer_features import ensure_job_feature_access
from app.api.routes.analysis_jobs import _read_job, viewer_owns_job
from app.core.temporal import format_temporal
from sqlalchemy.orm import Session

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("/analysis-jobs/{job_id}/executive-summary")
async def report_completed_analysis_job(
    job_id: str,
    request: Request,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    """Build a report from the saved job result and server provenance, never browser-supplied metrics."""
    try:
        job = _read_job(job_id)
    except HTTPException:
        raise
    provenance = db.get(AnalysisProvenance, job_id)
    if not viewer_owns_job(job_id, viewer, provenance.created_by_user_id if provenance else None):
        raise HTTPException(404, "Analisis tidak ditemukan.")
    if job.get("status") != "succeeded" or not isinstance(job.get("result"), dict):
        raise HTTPException(409, "Laporan hanya tersedia untuk analisis yang berhasil disimpan.")
    if provenance is None or provenance.status != "succeeded":
        raise HTTPException(409, "Provenance analisis belum tersedia; laporan belum dapat dibuat.")

    job_type = str(job.get("type") or "")
    parameters = provenance.parameters or {}
    ensure_job_feature_access(viewer, job_type, db, parameters)
    result = job["result"]
    analysis: dict = {}
    carbon_stats: dict = {}
    if job_type.startswith("carbon"):
        pools = result.get("pools") if isinstance(result.get("pools"), dict) else {}
        estimated = result.get("carbon_estimated") if isinstance(result.get("carbon_estimated"), dict) else {}
        if isinstance(estimated.get("statistics"), dict):
            carbon_stats = dict(estimated["statistics"])
        if not carbon_stats and pools:
            first = next((item for item in pools.values() if isinstance(item, dict)), None)
            if isinstance(first, dict):
                candidate = first.get("carbon_estimated")
                estimated = candidate if isinstance(candidate, dict) else {}
                statistics = estimated.get("statistics")
                carbon_stats = dict(statistics) if isinstance(statistics, dict) else {}
        model_info = result.get("model_info") if isinstance(result.get("model_info"), dict) else {}
        analysis["carbon"] = {
            "statistics": carbon_stats,
            "dataset": (result.get("carbon_reference") or {}).get("full_name")
            if isinstance(result.get("carbon_reference"), dict) else None,
            "model_name": model_info.get("model_name"),
            "target_pool": model_info.get("target_pool"),
        }
    elif job_type.startswith("landcover"):
        analysis["landcover"] = result
    elif job_type == "vegetation":
        analysis["vegetation"] = result

    provenance_payload = provenance.provenance if isinstance(provenance.provenance, dict) else {}
    manifest_value = provenance_payload.get("inference_manifest")
    manifest = manifest_value if isinstance(manifest_value, dict) else {}
    pinned_value = manifest.get("pinned_model")
    pinned_model = pinned_value if isinstance(pinned_value, dict) else {}
    model_value = manifest.get("model")
    manifest_model = model_value if isinstance(model_value, dict) else {}
    model_entry = {
        "name": pinned_model.get("name") or manifest_model.get("model_name"),
        "version": pinned_model.get("version") or manifest_model.get("model_version"),
        "artifact_sha256": pinned_model.get("artifact_sha256"),
    }
    metadata = {
        "datasets": [provenance.dataset_id] if provenance.dataset_id else [],
        "models": [model_entry] if model_entry.get("name") else [],
        "parameters": {
            "analysis_job_id": job_id,
            "analysis_type": job_type,
            "dataset_version": provenance.dataset_version,
            "crs": provenance.crs,
            "resolution": provenance.resolution,
            "cloud_mask": provenance.cloud_mask,
            "model_version": provenance.model_version,
            "artifact_checksums": provenance.artifact_checksums,
            "quality_flags": provenance.quality_flags,
            "confidence": provenance.confidence,
            "created_at": format_temporal(provenance.created_at, field="created_at"),
            "completed_at": format_temporal(provenance.completed_at, field="completed_at"),
        },
    }
    aoi_name = parameters.get("aoi_name") or parameters.get("analysis_area_name") or "Area Studi"
    period_year = parameters.get("year") or parameters.get("end_year")
    report_payload = {
        "title": f"Ringkasan Analisis SaveGeo · {aoi_name}",
        "aoi_name": str(aoi_name)[:160],
        "generated_by": getattr(viewer, "full_name", None) or getattr(viewer, "username", "SaveGeo"),
        "period": {"year": period_year} if period_year else {},
        "analysis": analysis,
        "metadata": metadata,
        "limitations": [
            "Laporan dibentuk dari hasil job tersimpan dan provenance server; hasil hanya berlaku untuk input, dataset, dan versi model yang tercatat.",
            "Validasi lapangan dan keterwakilan wilayah harus dibaca dari status mutu; metrik raster tidak menggantikan pengukuran lapangan.",
        ],
    }
    buf = await run_in_threadpool(build_executive_summary_docx, report_payload)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    aoi_slug = re.sub(r"[^a-zA-Z0-9_]", "_", str(aoi_name))[:30].strip("_") or "report"
    filename = f"savegeo_analysis_{aoi_slug}_{ts}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/executive-summary")
async def executive_summary_docx(
    request: Request,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    payload = await read_typed_json_object(request, ExecutiveSummaryRequest)
    result = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    included = {
        "carbon": "app.feature.carbon_stock",
        "landcover": "app.feature.land_cover",
        "vegetation": "app.feature.vegetation_indices",
    }
    requested = [code for key, code in included.items() if result.get(key)]
    for code in requested or ["app.feature.carbon_stock"]:
        ensure_feature_access(viewer, code, db)
    buf = await run_in_threadpool(build_executive_summary_docx, payload)

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    aoi_raw = str(payload.get("aoi_name") or "report")
    aoi_slug = re.sub(r"[^a-zA-Z0-9_]", "_", aoi_raw)[:30].strip("_") or "report"
    filename = f"savegeo_executive_summary_{aoi_slug}_{ts}.docx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
