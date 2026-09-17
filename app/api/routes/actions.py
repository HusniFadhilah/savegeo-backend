"""Durable action and output registry endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_app_viewer
from app.db.models.analysis_output import AnalysisOutput
from app.db.models.analysis_provenance import AnalysisProvenance
from app.db.session import get_db
from app.services.output_registry_service import get_action

router = APIRouter(tags=["actions"])


def _output_or_404(db: Session, action_id: str, output_id: str) -> AnalysisOutput:
    output = db.scalar(
        select(AnalysisOutput).where(AnalysisOutput.action_id == action_id, AnalysisOutput.id == output_id)
    )
    if output is None:
        raise HTTPException(status_code=404, detail="Output not found")
    return output


@router.get("/actions/{action_id}")
def get_action_contract(action_id: str, _viewer: object = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    action = get_action(db, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@router.get("/actions/{action_id}/outputs")
def list_action_outputs(action_id: str, _viewer: object = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    if db.get(AnalysisProvenance, action_id) is None:
        raise HTTPException(status_code=404, detail="Action not found")
    values = list(db.scalars(select(AnalysisOutput).where(AnalysisOutput.action_id == action_id)))
    return {"action_id": action_id, "outputs": [value.to_dict() for value in values]}


@router.get("/actions/{action_id}/outputs/{output_id}/metadata")
def get_output_metadata(action_id: str, output_id: str, _viewer: object = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    return _output_or_404(db, action_id, output_id).to_dict()


@router.get("/actions/{action_id}/outputs/{output_id}/content")
def get_output_content(action_id: str, output_id: str, _viewer: object = Depends(get_current_app_viewer), db: Session = Depends(get_db)):
    output = _output_or_404(db, action_id, output_id)
    if output.media_type == "application/json" and isinstance(output.metadata_json, dict) and output.profile == "tilejson-3.0.0":
        return JSONResponse(content=output.metadata_json, media_type="application/json")
    raise HTTPException(status_code=501, detail="Output content is stored externally or is still being generated")
