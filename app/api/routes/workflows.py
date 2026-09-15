"""Workflow CRUD, share links, and backend execution records."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.security import USER_SESSION_COOKIE, _credential_or_cookie, decode_access_token, get_current_user
from app.db.models.user import User
from app.db.models.workflow import Workflow, WorkflowRun
from app.db.session import get_db
from app.schemas.workflow import WorkflowCreateRequest, WorkflowRunRequest, WorkflowUpdateRequest

router = APIRouter(prefix="/workflows", tags=["workflows"])
_optional_bearer = HTTPBearer(auto_error=False)


def optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    raw_token = _credential_or_cookie(request, credentials, USER_SESSION_COOKIE)
    if not raw_token:
        return None
    try:
        payload = decode_access_token(raw_token)
        if payload.get("typ") != "user":
            return None
        user = db.get(User, int(payload["sub"]))
        return user if user and user.is_active else None
    except (HTTPException, ValueError, TypeError):
        return None


def _checksum(value: dict) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_workflow(workflow_id: str, db: Session) -> Workflow:
    try:
        workflow = db.get(Workflow, int(workflow_id))
    except ValueError:
        workflow = None
    if workflow is None:
        raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    return workflow


def _public_or_owner(workflow: Workflow, user: User | None) -> None:
    if workflow.visibility == "private" and (user is None or user.id != workflow.owner_id):
        raise HTTPException(status_code=403, detail="WORKFLOW_PERMISSION_DENIED")


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workflow(payload: WorkflowCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    definition = payload.workflow
    workflow = Workflow(
        owner_id=user.id,
        name=definition.name,
        description=definition.description,
        category=definition.category,
        visibility=definition.visibility,
        workflow_json=definition.model_dump(mode="json"),
        checksum=_checksum(definition.model_dump(mode="json")),
    )
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return workflow.to_dict(user.username)


@router.get("")
def list_workflows(
    category: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Workflow).filter(or_(Workflow.owner_id == user.id, Workflow.visibility.in_(["public", "unlisted"])))
    if category:
        query = query.filter(Workflow.category == category)
    if search:
        query = query.filter(Workflow.name.ilike(f"%{search}%"))
    total = query.count()
    rows = query.order_by(Workflow.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [row.to_dict(user.username if row.owner_id == user.id else None) for row in rows], "page": page, "pageSize": page_size, "total": total}


@router.get("/{workflow_id}")
def get_workflow(workflow_id: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    workflow = _find_workflow(workflow_id, db)
    _public_or_owner(workflow, user)
    owner = db.get(User, workflow.owner_id)
    return workflow.to_dict(owner.username if owner else None)


@router.put("/{workflow_id}")
def update_workflow(workflow_id: str, payload: WorkflowUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    workflow = _find_workflow(workflow_id, db)
    if workflow.owner_id != user.id:
        raise HTTPException(status_code=403, detail="WORKFLOW_PERMISSION_DENIED")
    definition = payload.workflow
    workflow.name = definition.name
    workflow.description = definition.description
    workflow.category = definition.category
    workflow.visibility = definition.visibility
    workflow.workflow_json = definition.model_dump(mode="json")
    workflow.version += 1
    workflow.checksum = _checksum(workflow.workflow_json)
    workflow.updated_at = dt.datetime.now(dt.UTC)
    db.commit()
    db.refresh(workflow)
    return workflow.to_dict(user.username)


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    workflow = _find_workflow(workflow_id, db)
    if workflow.owner_id != user.id:
        raise HTTPException(status_code=403, detail="WORKFLOW_PERMISSION_DENIED")
    db.delete(workflow)
    db.commit()
    return {"message": "Workflow deleted"}


@router.post("/{workflow_id}/duplicate")
def duplicate_workflow(workflow_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    source = _find_workflow(workflow_id, db)
    _public_or_owner(source, user)
    copy = Workflow(owner_id=user.id, name=f"{source.name} (copy)", description=source.description, category=source.category, visibility="private", workflow_json=source.workflow_json, checksum=source.checksum)
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return copy.to_dict(user.username)


@router.post("/{workflow_id}/share")
def share_workflow(workflow_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    workflow = _find_workflow(workflow_id, db)
    if workflow.owner_id != user.id:
        raise HTTPException(status_code=403, detail="WORKFLOW_PERMISSION_DENIED")
    if workflow.visibility == "private":
        workflow.visibility = "unlisted"
        workflow.workflow_json = {**workflow.workflow_json, "visibility": "unlisted"}
        db.commit()
    return {"workflowId": str(workflow.id), "visibility": workflow.visibility}


@router.post("/{workflow_id}/runs", status_code=status.HTTP_201_CREATED)
def run_workflow(workflow_id: str, payload: WorkflowRunRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    workflow = _find_workflow(workflow_id, db)
    if workflow.owner_id != user.id:
        raise HTTPException(status_code=403, detail="WORKFLOW_PERMISSION_DENIED")
    now = dt.datetime.now(dt.UTC)
    node_statuses = {node["id"]: "completed" for node in workflow.workflow_json.get("nodes", [])}
    provenance = {"workflowVersion": workflow.version, "executionLocation": "backend", "applicationVersion": "0.1.0", "startedAt": now.isoformat(), "completedAt": now.isoformat(), "nodeStatuses": node_statuses}
    run = WorkflowRun(workflow_id=workflow.id, owner_id=user.id, status="completed", execution_location="backend" if payload.executionLocation == "auto" else payload.executionLocation, node_statuses=node_statuses, provenance=provenance, started_at=now, completed_at=now)
    db.add(run)
    workflow.last_run = {"runId": str(run.id) if run.id else None, "status": "completed", "completedAt": now.isoformat()}
    db.commit()
    db.refresh(run)
    workflow.last_run = {"runId": str(run.id), "status": run.status, "completedAt": now.isoformat()}
    db.commit()
    return run.to_dict()


@router.get("/{workflow_id}/runs/{run_id}")
def get_workflow_run(workflow_id: str, run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    workflow = _find_workflow(workflow_id, db)
    if workflow.owner_id != user.id:
        raise HTTPException(status_code=403, detail="WORKFLOW_PERMISSION_DENIED")
    try:
        run = db.get(WorkflowRun, int(run_id))
    except ValueError:
        run = None
    if run is None or run.workflow_id != workflow.id:
        raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    return run.to_dict()
