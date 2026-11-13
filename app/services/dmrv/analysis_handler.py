"""Adapter from the durable queue to existing scientific application services."""
from __future__ import annotations

import json
import uuid

import requests
from fastapi import HTTPException
from sqlalchemy import select

from app.core.viewer_features import ensure_job_feature_access
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.user import User
from app.db.models.workspace_analysis import WorkspaceAnalysisRun
from app.services.dmrv.authz import authorize
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.job_handlers import JobContext, JobFailed, RetryableJobError
from app.services.workspace_analysis import digest


def validate(payload: dict) -> dict:
    if set(payload) != {"context"} or not isinstance(payload["context"], dict):
        raise ValueError("Server-resolved project context is required")
    return payload


def execute(db, kind: str, parameters: dict) -> dict:
    # These are the same services used by exploration, not a second algorithm.
    from app.services import carbon_service, landcover_service, vegetation_service
    if kind == "carbon":
        return carbon_service.analyze_carbon(db, parameters)
    if kind == "vegetation":
        return vegetation_service.analyze_vegetation(db, parameters)
    if kind == "landcover":
        return landcover_service.analyze_landcover(parameters)
    if kind == "landcover_transition":
        return landcover_service.analyze_landcover_transition(parameters)
    raise JobFailed("unsupported_analysis", "Jenis analisis tidak tersedia.")


def run(ctx: JobContext) -> dict:
    from app.services import gee_service
    from app.services.gee_common import AnalysisError, public_analysis_error

    ctx.stage("authorize")
    run_record = ctx.db.get(WorkspaceAnalysisRun, ctx.job_id)
    if run_record is None or digest(run_record.context) != run_record.context_hash or ctx.payload != {"context": run_record.context}:
        raise JobFailed("context_mismatch", "Konteks pekerjaan tidak cocok dengan catatan server.")
    context = run_record.context
    kind = ctx.db.get(DmrvJob, ctx.job_id).kind.removeprefix("analysis.")
    parameters = context["parameters"]
    account_class = User if ctx.principal.type == "user" else AdminUser
    account = ctx.db.get(account_class, ctx.principal.id)
    try:
        if account is None or not account.is_active:
            raise JobFailed("account_inactive", "Akun pembuat pekerjaan tidak aktif.")
        authorize(ctx.db, ctx.project_id, ctx.principal, "analysis.run")
        ensure_job_feature_access(account, kind, ctx.db, parameters)
    except (DmrvError, HTTPException):
        raise JobFailed("access_revoked", "Izin menjalankan analisis sudah tidak tersedia.") from None
    ctx.db.rollback()  # release read transaction before slow provider calls
    ctx.checkpoint()
    ctx.stage("initialize_provider")
    # initialize_ee may update credential last-used; use the control session,
    # never the transaction that will publish the immutable result.
    if not gee_service.initialize_ee(ctx.control):
        raise RetryableJobError("provider_unavailable", "Earth Engine belum siap. Periksa kredensial layanan.")
    ctx.stage("analyze")
    try:
        result = execute(ctx.db, kind, parameters)
    except AnalysisError as exc:
        error = RetryableJobError if exc.status_code in {429, 502, 503, 504} else JobFailed
        raise error("analysis_failed", public_analysis_error(exc)) from None
    except requests.RequestException:
        raise RetryableJobError("provider_connection", "Koneksi penyedia data terputus; pekerjaan akan dicoba ulang.") from None
    ctx.checkpoint()
    ctx.stage("snapshot")
    # Resolve permissions again after inference: membership can change while a
    # provider request is running. No snapshot is staged until this check passes.
    ctx.db.expire_all()
    try:
        authorize(ctx.db, ctx.project_id, ctx.principal, "analysis.run")
        account = ctx.db.get(account_class, ctx.principal.id)
        if account is None or not account.is_active:
            raise JobFailed("account_inactive", "Akun pembuat pekerjaan tidak aktif.")
        ensure_job_feature_access(account, kind, ctx.db, parameters)
    except (DmrvError, HTTPException):
        raise JobFailed("access_revoked", "Izin analisis dicabut sebelum hasil diterbitkan.") from None
    outputs = json.loads(json.dumps(result, allow_nan=False))
    inputs = {"context": context, "context_hash": digest(context), "adapter_version": "project_analysis_v1"}
    snapshot_hash = digest({"kind": kind, "inputs": inputs, "outputs": outputs})
    snapshot = ctx.db.scalar(select(DmrvSnapshot).where(DmrvSnapshot.project_id == ctx.project_id, DmrvSnapshot.snapshot_hash == snapshot_hash))
    if snapshot is None:
        snapshot = DmrvSnapshot(id=str(uuid.uuid4()), project_id=ctx.project_id, job_id=ctx.job_id,
                                kind=kind, inputs=inputs, outputs=outputs, snapshot_hash=snapshot_hash, created_by=ctx.created_by)
        ctx.db.add(snapshot)
        ctx.db.flush()
    return {"snapshot_id": snapshot.id, "snapshot_hash": snapshot_hash, "context_hash": digest(context)}
