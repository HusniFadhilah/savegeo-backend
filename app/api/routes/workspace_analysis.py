"""Durable project analysis and usage; every operation rechecks membership."""
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.core.viewer_features import JOB_TYPE_FEATURES, viewer_feature_codes
from app.db.models.dmrv_job import DmrvJob
from app.db.models.workspace_analysis import WorkspaceAnalysisRun
from app.db.session import get_db
from app.schemas.dmrv import DmrvJobOut
from app.schemas.workspace_analysis import ProjectAnalysisCreate
from app.services import workspace_analysis, workspace_usage
from app.services.dmrv import jobs
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError
from app.services.organizations import require_member

router = APIRouter(tags=["workspace-analysis"])


def out(job: DmrvJob) -> DmrvJobOut:
    return DmrvJobOut.model_validate(job, from_attributes=True)


@router.post("/dmrv/projects/{project_id}/analysis-runs", response_model=DmrvJobOut)
def submit(project_id: str, payload: ProjectAnalysisCreate, request: Request, response: Response,
           idempotency_key: str = Header(alias="Idempotency-Key"),
           viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    row, replay = workspace_analysis.submit(db, viewer, project_id, payload, idempotency_key,
                                           getattr(request.state, "request_id", None))
    response.status_code = 200 if replay else 202
    return out(row)


@router.get("/dmrv/projects/{project_id}/analysis-runs")
def list_runs(project_id: str, monitoring_period_id: str | None = None, limit: int = Query(20, ge=1, le=100),
              offset: int = Query(0, ge=0), viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    authorize(db, project_id, Principal.from_account(viewer), "project.read")
    allowed = ["analysis." + kind for kind, code in JOB_TYPE_FEATURES.items() if code in viewer_feature_codes(db, viewer)]
    query = select(DmrvJob, WorkspaceAnalysisRun).join(WorkspaceAnalysisRun, WorkspaceAnalysisRun.job_id == DmrvJob.id).where(
        DmrvJob.project_id == project_id, DmrvJob.kind.in_(allowed))
    if monitoring_period_id:
        query = query.where(WorkspaceAnalysisRun.period_id == monitoring_period_id)
    rows = db.execute(query.order_by(DmrvJob.created_at.desc(), DmrvJob.id).offset(offset).limit(limit)).all()
    return [{"job": out(job), "context": run.context, "context_hash": run.context_hash} for job, run in rows]


@router.post("/dmrv/projects/{project_id}/analysis-runs/{job_id}/cancel", response_model=DmrvJobOut)
def cancel(project_id: str, job_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    principal = Principal.from_account(viewer)
    authorize(db, project_id, principal, "analysis.run")
    row = db.scalar(select(DmrvJob).where(DmrvJob.id == job_id, DmrvJob.project_id == project_id,
                                         DmrvJob.kind.like("analysis.%")).with_for_update())
    if row is None:
        raise DmrvError(404, "not_found", "Pekerjaan tidak ditemukan.")
    record_audit(db, principal=principal, action="analysis.cancel", resource_type="dmrv_job", resource_id=job_id,
                 project_id=project_id, before={"status": row.status}, after={"cancel_requested": True})
    jobs.request_cancel(db, job_id)
    db.refresh(row)
    return out(row)


@router.get("/organizations/{organization_id}/usage")
def usage(organization_id: str, viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    require_member(db, organization_id, Principal.from_account(viewer))
    return workspace_usage.summary(db, organization_id)
